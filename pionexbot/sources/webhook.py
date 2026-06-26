"""Webhook 接收伺服器：接收 TradingView 或任何來源的 HTTP POST 訊號。

TradingView 警報訊息範例（在警報的 Message 欄位填 JSON）：
    {"secret": "你的密鑰", "action": "BUY", "symbol": "BTC_USDT", "quote_amount": 20}
    {"secret": "你的密鑰", "action": "SELL", "symbol": "BTC_USDT"}

也接受 TradingView 內建的 {{strategy.order.action}} 形式：
    {"secret": "...", "action": "{{strategy.order.action}}", "symbol": "{{ticker}}"}
"""
from __future__ import annotations

import hmac
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import Config
from ..executor import Executor
from ..models import Action, Signal
from ..notifier import Notifier


def _parse_action(value: Any) -> Optional[Action]:
    if value is None:
        return None
    v = str(value).strip().upper()
    mapping = {
        "BUY": Action.BUY, "LONG": Action.BUY, "ENTRY": Action.BUY,
        "SELL": Action.SELL, "EXIT": Action.CLOSE, "CLOSE": Action.CLOSE,
        "SHORT": Action.SELL,
    }
    return mapping.get(v)


def build_app(cfg: Config, executor: Executor, notifier: Notifier) -> FastAPI:
    app = FastAPI(title="派網訊號機器人 Webhook")
    path = cfg.webhook.get("path", "/webhook")
    secret = cfg.secrets.webhook_secret
    default_symbol = cfg.symbol
    default_quote = float(cfg.trading.get("quote_per_trade", 20))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "mode": cfg.mode}

    @app.post(path)
    async def receive(request: Request) -> JSONResponse:
        # 解析 body（容忍純文字 JSON）
        try:
            payload = await request.json()
        except Exception:
            body = (await request.body()).decode("utf-8", "ignore")
            notifier.send(f"Webhook 非 JSON 內容被拒：{body[:120]}", "warning")
            return JSONResponse({"ok": False, "error": "需要 JSON"}, status_code=400)

        # 驗證共享密鑰（避免被陌生人觸發下單）
        if secret:
            given = str(payload.get("secret", ""))
            if not hmac.compare_digest(given, secret):
                notifier.send("Webhook 密鑰錯誤，已拒絕", "warning")
                return JSONResponse({"ok": False, "error": "密鑰錯誤"}, status_code=401)

        action = _parse_action(payload.get("action"))
        if action is None:
            return JSONResponse(
                {"ok": False, "error": f"無法辨識 action：{payload.get('action')!r}"},
                status_code=400)

        symbol = str(payload.get("symbol") or default_symbol).upper()
        signal = Signal(
            action=action, symbol=symbol, source="webhook:tradingview",
            quote_amount=_to_float(payload.get("quote_amount")),
            base_size=_to_float(payload.get("base_size")),
            price=_to_float(payload.get("price")),
            reason=str(payload.get("reason", "webhook 訊號")),
            raw=payload,
        )
        if action == Action.BUY and signal.quote_amount is None:
            signal.quote_amount = default_quote

        notifier.send(f"📨 收到 Webhook 訊號：{signal}", "info")
        result = executor.handle(signal)
        return JSONResponse({
            "ok": bool(result and result.ok),
            "detail": str(result) if result else "已接收但未成交（HOLD 或被風控擋下）",
        })

    return app


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
