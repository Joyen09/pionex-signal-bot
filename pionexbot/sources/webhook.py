"""Webhook 接收伺服器：接收 TradingView 或任何來源的 HTTP POST 訊號。

TradingView 警報訊息範例（在警報的 Message 欄位填 JSON）：
    {"secret": "你的密鑰", "action": "BUY", "symbol": "BTC_USDT", "quote_amount": 20}
    {"secret": "你的密鑰", "action": "SELL", "symbol": "BTC_USDT"}

進階：帶止損 / 多段停利（以止損決定倉位，見 risk.py）：
    {"secret": "...", "action": "BUY", "symbol": "BTC_USDT",
     "sl": 64000,
     "tps": [{"price": 68000, "fraction": 0.5}, {"price": 70000, "fraction": 0.5}],
     "risk_quote": 50}

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
from ..models import Action, Signal, TakeProfit
from ..notifier import Notifier


def parse_plan_fields(payload: dict) -> tuple[Optional[float],
                                              Optional[list[TakeProfit]],
                                              Optional[float]]:
    """解析並驗證 sl / tps / risk_quote。不合法時 raise ValueError。

    價格與進場方向的關係（SL 需低於進場價）由風控在下單當下用市價驗證。"""
    sl = _to_float(payload.get("sl"))
    risk_quote = _to_float(payload.get("risk_quote"))
    if risk_quote is not None and risk_quote <= 0:
        raise ValueError(f"risk_quote 必須為正數：{risk_quote}")

    tps_raw = payload.get("tps")
    tps: Optional[list[TakeProfit]] = None
    if tps_raw is not None:
        if not isinstance(tps_raw, list) or not tps_raw:
            raise ValueError("tps 必須是非空陣列")
        tps = []
        total = 0.0
        for item in tps_raw:
            price = _to_float(item.get("price")) if isinstance(item, dict) else None
            fraction = _to_float(item.get("fraction")) if isinstance(item, dict) else None
            if not price or price <= 0 or not fraction or fraction <= 0:
                raise ValueError(f"停利段不合法：{item!r}")
            if sl is not None and price <= sl:
                raise ValueError(f"停利價 {price} 必須高於止損 {sl}")
            total += fraction
            tps.append(TakeProfit(price=price, fraction=fraction))
        if total > 1.0 + 1e-9:
            raise ValueError(f"停利比例總和 {total:.2f} 超過 1")
    return sl, tps, risk_quote


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
        try:
            sl, tps, risk_quote = parse_plan_fields(payload)
        except ValueError as exc:
            notifier.send(f"Webhook SL/TP 欄位不合法：{exc}", "warning")
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        signal = Signal(
            action=action, symbol=symbol, source="webhook:tradingview",
            quote_amount=_to_float(payload.get("quote_amount")),
            base_size=_to_float(payload.get("base_size")),
            price=_to_float(payload.get("price")),
            reason=str(payload.get("reason", "webhook 訊號")),
            raw=payload,
            stop_loss=sl, take_profits=tps, risk_quote=risk_quote,
        )
        # 帶止損時倉位由風控計算，不必補預設金額
        if action == Action.BUY and signal.quote_amount is None and sl is None:
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
