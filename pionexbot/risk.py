"""風控：在下單前檢查訊號是否符合安全限制。"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

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

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def roll_day(self, pos: Position) -> None:
        """跨日時重置當日損益。"""
        today = self._today()
        if pos.day != today:
            pos.day = today
            pos.realized_pnl_today = 0.0

    def check(self, signal: Signal, pos: Position, price: float) -> RiskDecision:
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
