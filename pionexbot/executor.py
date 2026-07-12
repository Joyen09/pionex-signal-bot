"""執行層：訊號 -> 風控 -> 下單 -> 更新持倉/損益 -> 記錄/通知。

帶止損的訊號（signal.stop_loss）會建立「出場計畫」：
- 進場後把 SL / 多段 TP 存進 store（重啟不遺失）。
- runner 每輪呼叫 check_plan()：
    * TP 觸價（touch-based）→ 市價賣出該段比例；TP1 成交後可把 SL 移到保本。
    * SL 收盤觸發（close-based）→ 收盤價 <= SL 就市價平掉剩餘部位。
    * 同一根 K 同時滿足 TP 與 SL → 以 SL 計（保守，fill_ambiguity: worst）。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from .broker import Broker
from .models import Action, OrderResult, Signal
from .notifier import Notifier
from .risk import RiskManager
from .store import Store


class Executor:
    def __init__(self, broker: Broker, risk: RiskManager, store: Store,
                 notifier: Notifier, risk_cfg: Optional[dict] = None):
        self.broker = broker
        self.risk = risk
        self.store = store
        self.notifier = notifier
        rc = risk_cfg or {}
        # 保本設定：TP 第 N 段成交後，把 SL 移到 entry*(1+offset)
        self.breakeven_after_tp = int(rc.get("breakeven_after_tp", 1))
        self.breakeven_offset_pct = float(rc.get("breakeven_offset_pct", 0.001))
        # 紙上模式沒有真實餘額可查時，用這個當 equity 算 risk_per_trade_pct
        pe = rc.get("paper_equity")
        self.paper_equity: Optional[float] = float(pe) if pe else None

    # ------------------------------------------------------------------ #
    # 輔助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _midnight_utc_ts() -> float:
        now = datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def _equity(self, price: float) -> Optional[float]:
        """估算帳戶權益（USDT）：實盤查餘額；紙上用設定值。取不到回 None。"""
        client = getattr(self.broker, "client", None)
        if not self.broker.simulated and client is not None:
            try:
                balances = client.get_balances()
                usdt = float(balances.get("USDT", 0))
                pos = self.store.load_position()
                return usdt + pos.base * price
            except Exception:  # noqa: BLE001 - 查不到就退回設定值
                pass
        return self.paper_equity

    # ------------------------------------------------------------------ #
    # 訊號處理
    # ------------------------------------------------------------------ #
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

        decision = self.risk.check(
            signal, pos, price,
            equity=self._equity(price) if signal.action == Action.BUY else None,
            entries_today=self.store.count_buys_since(self._midnight_utc_ts()),
        )
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

        # 帶止損的買入 → 建立出場計畫（SL / 多段 TP / 保本）
        if signal.action == Action.BUY and signal.stop_loss is not None:
            self._open_plan(signal, result)
        elif signal.action in (Action.SELL, Action.CLOSE):
            # 手動/策略全平 → 計畫作廢
            if self.store.load_position().base <= 0:
                self.store.clear_plan()

        # important=True → 實際成交會推 LINE/Telegram 通知
        self.notifier.send(f"✅ {result} | 觸發：{signal.reason or signal.source}",
                           "info", important=True)
        return result

    def _open_plan(self, signal: Signal, result: OrderResult) -> None:
        plan = {
            "symbol": signal.symbol,
            "entry": result.avg_price,
            "stop_loss": signal.stop_loss,
            "qty_initial": result.filled_base,
            "tps": [{"price": tp.price, "fraction": tp.fraction, "filled": False}
                    for tp in (signal.take_profits or [])],
            "breakeven_done": False,
            "source": signal.source,
            "tags": signal.tags or {},
        }
        self.store.save_plan(plan)
        tp_txt = ("；TP " + " / ".join(
            f"{t['price']:.2f}×{t['fraction']}" for t in plan["tps"])
            ) if plan["tps"] else ""
        self.notifier.send(
            f"🛡 出場計畫建立：進場 {plan['entry']:.2f}、SL {plan['stop_loss']:.2f}"
            f"{tp_txt}", "info", important=True)

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

    # ------------------------------------------------------------------ #
    # 出場計畫監控（runner 每輪呼叫）
    # ------------------------------------------------------------------ #
    def check_plan(self, price: float,
                   candle_close: Optional[float] = None) -> None:
        """檢查出場計畫。

        price        ：目前市價，用於 TP 觸價（touch-based）。
        candle_close ：最新「已收盤」K 線的收盤價；只在有新收盤 K 時傳入，
                       用於 SL 收盤觸發（close-based）。
        同根衝突以 SL 計 → 先檢查 SL、後檢查 TP。
        """
        plan = self.store.load_plan()
        if not plan:
            return
        pos = self.store.load_position()
        if pos.base <= 0:
            self.store.clear_plan()
            return
        symbol = plan["symbol"]
        sl = float(plan["stop_loss"])

        # 1) SL：收盤觸發（先於 TP → 同根衝突以 SL 計）
        if candle_close is not None and candle_close <= sl:
            res = self.broker.market_sell(symbol, pos.base)
            if res.ok:
                self._apply_fill(pos, res, Signal(
                    Action.CLOSE, symbol, source="plan:sl",
                    reason=f"止損（收盤 {candle_close:.2f} <= SL {sl:.2f}）"))
                self.store.clear_plan()
                self.notifier.send(
                    f"🛑 止損出場：收盤 {candle_close:.2f} <= SL {sl:.2f}，"
                    f"平掉剩餘 {res.filled_base:.8f} @ {res.avg_price:.2f}",
                    "info", important=True)
            else:
                self.notifier.send(f"❌ 止損平倉失敗：{res.error}", "error")
            return

        # 2) TP：觸價成交（依 fraction 分批，比例以「原始倉位」計）
        qty_initial = float(plan["qty_initial"])
        changed = False
        filled_count = sum(1 for t in plan["tps"] if t["filled"])
        for t in plan["tps"]:
            if t["filled"] or price < float(t["price"]):
                continue
            sell_qty = min(qty_initial * float(t["fraction"]), pos.base)
            if sell_qty <= 0:
                t["filled"] = True
                changed = True
                continue
            res = self.broker.market_sell(symbol, sell_qty)
            if not res.ok:
                self.notifier.send(f"❌ 停利賣出失敗：{res.error}", "error")
                break
            self._apply_fill(pos, res, Signal(
                Action.SELL, symbol, source="plan:tp",
                reason=f"停利 TP@{t['price']}"))
            pos = self.store.load_position()
            t["filled"] = True
            filled_count += 1
            changed = True
            self.notifier.send(
                f"🎯 停利成交：賣出 {res.filled_base:.8f} @ {res.avg_price:.2f}"
                f"（TP {t['price']}，第 {filled_count} 段）", "info", important=True)

            # 保本：TP 第 N 段成交後，SL 移到 entry*(1+offset)（只上移不下移）
            if (not plan["breakeven_done"]
                    and self.breakeven_after_tp > 0
                    and filled_count >= self.breakeven_after_tp):
                new_sl = float(plan["entry"]) * (1 + self.breakeven_offset_pct)
                if new_sl > float(plan["stop_loss"]):
                    plan["stop_loss"] = new_sl
                    self.notifier.send(
                        f"🔒 移動保本：SL 上移至 {new_sl:.2f}", "info", important=True)
                plan["breakeven_done"] = True

        if pos.base <= 0:
            self.store.clear_plan()
            self.notifier.send("✅ 出場計畫全數完成，部位已出清。", "info", important=True)
        elif changed:
            self.store.save_plan(plan)
