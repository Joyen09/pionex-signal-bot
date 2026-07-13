"""風控：在下單前檢查訊號是否符合安全限制。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Action, Signal
from .store import Position


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""
    # 經風控調整後的實際數量（例如把金額壓到上限內）
    quote_amount: float = 0.0
    base_size: float = 0.0


class RiskManager:
    def __init__(self, cfg: dict):
        self.max_quote = float(cfg.get("max_quote_per_trade", 50))
        self.max_position_base = float(cfg.get("max_position_base", 0.01))
        self.daily_loss_limit = float(cfg.get("daily_loss_limit_quote", 50))
        self.cooldown = float(cfg.get("cooldown_seconds", 60))
        self.allow_short = bool(cfg.get("allow_short", False))
        # --- 以止損決定倉位 ---
        self.risk_per_trade_pct = float(cfg.get("risk_per_trade_pct", 0.01) or 0)
        rq = cfg.get("risk_quote_default")
        self.risk_quote_default: Optional[float] = float(rq) if rq else None
        # --- 每日開單上限 ---
        self.max_trades_per_day = int(cfg.get("max_trades_per_day", 3))
        # --- 消息面封鎖 ---
        self.news_calendar_path = str(cfg.get("news_calendar_path",
                                              "data/news_calendar.yaml"))
        self.news_close_positions = bool(cfg.get("news_close_positions", False))
        self._news_cache: tuple[float, list] = (0.0, [])  # (mtime, events)

    # ------------------------------------------------------------------ #
    # 以止損決定倉位
    # ------------------------------------------------------------------ #
    def compute_risk_quote(self, signal: Signal,
                           equity: Optional[float]) -> Optional[float]:
        """本筆願意虧的 USDT：Signal 帶值 > equity 百分比 > 固定預設。都沒有則 None。"""
        if signal.risk_quote and signal.risk_quote > 0:
            return float(signal.risk_quote)
        if equity and equity > 0 and self.risk_per_trade_pct > 0:
            return equity * self.risk_per_trade_pct
        return self.risk_quote_default

    @staticmethod
    def size_by_stop(entry: float, stop_loss: float, risk_quote: float) -> float:
        """倉位數量 = 願意虧的錢 / 每單位虧損。呼叫前需保證 stop_loss < entry。"""
        return risk_quote / (entry - stop_loss)

    @staticmethod
    def validate_plan(signal: Signal, entry: float) -> Optional[str]:
        """檢查 SL/TP 是否合法（做多）。回傳錯誤訊息，None 代表合法。"""
        if signal.stop_loss is not None and signal.stop_loss >= entry:
            return (f"止損 {signal.stop_loss} 必須低於進場價 {entry}"
                    "（做多的止損要在下方）")
        if signal.take_profits:
            total = 0.0
            for tp in signal.take_profits:
                if tp.fraction <= 0 or tp.price <= 0:
                    return f"停利段不合法：price={tp.price}, fraction={tp.fraction}"
                if tp.price <= entry:
                    return f"停利價 {tp.price} 必須高於進場價 {entry}"
                total += tp.fraction
            if total > 1.0 + 1e-9:
                return f"停利比例總和 {total:.2f} 超過 1"
        return None

    # ------------------------------------------------------------------ #
    # 消息面封鎖
    # ------------------------------------------------------------------ #
    def _load_news_events(self) -> list:
        """讀取消息日曆（YAML），以檔案 mtime 快取，檔案不存在則視為無事件。"""
        p = Path(self.news_calendar_path)
        if not p.exists():
            return []
        mtime = p.stat().st_mtime
        if mtime == self._news_cache[0]:
            return self._news_cache[1]
        try:
            import yaml
            with p.open("r", encoding="utf-8") as fh:
                events = yaml.safe_load(fh) or []
            if not isinstance(events, list):
                events = []
        except Exception:  # noqa: BLE001 - 日曆壞掉不該擋住交易主流程
            events = []
        self._news_cache = (mtime, events)
        return events

    def news_blocked(self, now: Optional[datetime] = None) -> Optional[str]:
        """目前是否在某個消息封鎖視窗內。回傳事件名稱，None 代表沒被封鎖。"""
        now = now or datetime.now(timezone.utc)
        for ev in self._load_news_events():
            try:
                t = datetime.fromisoformat(str(ev["time_utc"]).replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                before = float(ev.get("block_before_min", 30)) * 60
                after = float(ev.get("block_after_min", 30)) * 60
            except (KeyError, TypeError, ValueError):
                continue
            if (t.timestamp() - before) <= now.timestamp() <= (t.timestamp() + after):
                return str(ev.get("name", "未命名事件"))
        return None

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def roll_day(self, pos: Position) -> None:
        """跨日時重置當日損益。"""
        today = self._today()
        if pos.day != today:
            pos.day = today
            pos.realized_pnl_today = 0.0

    def check(self, signal: Signal, pos: Position, price: float,
              equity: Optional[float] = None,
              entries_today: int = 0) -> RiskDecision:
        self.roll_day(pos)
        now = time.time()

        # 冷卻時間
        if now - pos.last_trade_ts < self.cooldown:
            wait = self.cooldown - (now - pos.last_trade_ts)
            return RiskDecision(False, f"冷卻中，還需 {wait:.0f} 秒")

        # 當日虧損上限
        if pos.realized_pnl_today <= -abs(self.daily_loss_limit):
            return RiskDecision(False,
                f"當日虧損已達上限 {self.daily_loss_limit}，今日停止交易")

        if signal.action == Action.BUY:
            # 每日開單上限（只擋新進場，不影響出場）
            if self.max_trades_per_day > 0 and entries_today >= self.max_trades_per_day:
                return RiskDecision(False,
                    f"今日已開 {entries_today} 單，達每日上限 {self.max_trades_per_day}")
            # 消息面封鎖（視窗內拒絕新進場）
            blocked = self.news_blocked()
            if blocked:
                return RiskDecision(False, f"消息封鎖中（{blocked}），暫停新進場")

            # 有帶止損 → 以止損決定倉位
            if signal.stop_loss is not None:
                err = self.validate_plan(signal, price)
                if err:
                    return RiskDecision(False, err)
                risk_quote = self.compute_risk_quote(signal, equity)
                if not risk_quote or risk_quote <= 0:
                    return RiskDecision(False,
                        "帶止損的訊號需要 risk_quote（訊號帶值、或設定 "
                        "risk_per_trade_pct / risk_quote_default）")
                qty = self.size_by_stop(price, signal.stop_loss, risk_quote)
                quote = qty * price
            else:
                quote = signal.quote_amount or 0.0
                if quote <= 0:
                    return RiskDecision(False, "買入訊號缺少金額")

            if quote > self.max_quote:
                quote = self.max_quote  # 壓到上限而非直接拒絕
            # 持倉上限：估算買入後的部位
            if price > 0:
                est_base_after = pos.base + quote / price
                if est_base_after > self.max_position_base:
                    room = self.max_position_base - pos.base
                    if room <= 0:
                        return RiskDecision(False,
                            f"已達持倉上限 {self.max_position_base}")
                    quote = min(quote, room * price)
            return RiskDecision(True, quote_amount=quote)

        if signal.action in (Action.SELL, Action.CLOSE):
            # 賣出/平倉：賣掉指定數量或全部持倉
            base = signal.base_size if signal.base_size else pos.base
            if signal.action == Action.CLOSE:
                base = pos.base
            if base <= 0:
                if self.allow_short:
                    return RiskDecision(True, base_size=signal.base_size or 0.0)
                return RiskDecision(False, "沒有持倉可賣（現貨不可做空）")
            base = min(base, pos.base) if not self.allow_short else base
            return RiskDecision(True, base_size=base)

        return RiskDecision(False, f"未處理的動作：{signal.action}")
