"""執行層：訊號 -> 風控 -> 下單 -> 更新持倉/損益 -> 記錄/通知。"""
from __future__ import annotations

import time

from .broker import Broker
from .models import Action, OrderResult, Signal
from .notifier import Notifier
from .risk import RiskManager
from .store import Store


class Executor:
    def __init__(self, broker: Broker, risk: RiskManager, store: Store,
                 notifier: Notifier):
        self.broker = broker
        self.risk = risk
        self.store = store
        self.notifier = notifier

    def handle(self, signal: Signal) -> OrderResult | None:
        """處理單一訊號，回傳下單結果（被風控擋下或 HOLD 則回 None）。"""
        if signal.action == Action.HOLD:
            return None

        pos = self.store.load_position()
        try:
            price = self.broker.get_price(signal.symbol)
        except Exception as exc:  # noqa: BLE001 - 行情錯誤不該讓整個流程崩潰
            self.notifier.send(f"取得 {signal.symbol} 行情失敗：{exc}", "error")
            return None

        decision = self.risk.check(signal, pos, price)
        if not decision.allowed:
            self.notifier.send(f"⏸ 訊號被風控擋下：{signal} → {decision.reason}", "info")
            return None

        # 下單
        if signal.action == Action.BUY:
            result = self.broker.market_buy(signal.symbol, decision.quote_amount)
        else:  # SELL / CLOSE
            result = self.broker.market_sell(signal.symbol, decision.base_size)

        if not result.ok:
            self.notifier.send(f"❌ {result}", "error")
            return result

        self._apply_fill(pos, result, signal)
        self.notifier.send(f"✅ {result} | 觸發：{signal.reason or signal.source}", "info")
        return result

    def _apply_fill(self, pos, result: OrderResult, signal: Signal) -> None:
        """根據成交更新持倉、平均成本與已實現損益。"""
        self.risk.roll_day(pos)
        realized = 0.0

        if result.side.value == "BUY":
            total_cost = pos.avg_cost * pos.base + result.filled_quote
            pos.base += result.filled_base
            pos.avg_cost = total_cost / pos.base if pos.base > 0 else 0.0
        else:  # SELL
            # 已實現損益 = (賣出均價 - 持倉均價) * 賣出數量
            realized = (result.avg_price - pos.avg_cost) * result.filled_base
            pos.base -= result.filled_base
            if pos.base <= 1e-12:
                pos.base = 0.0
                pos.avg_cost = 0.0
            pos.realized_pnl_today += realized

        pos.last_trade_ts = time.time()
        self.store.save_position(pos)
        self.store.record_trade(
            symbol=result.symbol, side=result.side.value,
            base=result.filled_base, quote=result.filled_quote,
            price=result.avg_price, simulated=result.simulated,
            source=signal.source, order_id=result.order_id, realized_pnl=realized,
        )
