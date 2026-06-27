"""自動網格交易執行器。

行為：
- 在現價附近建立網格（區間均分成數格）。
- 每輪：價格下跌觸及某格 → 市價買入該格；持有的格子，價格漲到上一格 → 賣出獲利。
- 風險控管（自動）：
    * 價格跌破區間下緣（含緩衝）或未實現虧損超過上限 → 平倉關閉整個網格，
      然後在「現價」重新開一個新網格繼續交易。
    * 價格突破區間上緣 → 獲利了結後向上重新定位。
- 狀態（區間、持有格、已實現利潤）存進 store，重啟後可接續。

注意：自製版用市價單，手續費/滑價較高；網格利潤本來就薄，要做真錢建議優先用派網內建網格。
"""
from __future__ import annotations

import time
from typing import Optional

from ..config import Config
from ..notifier import Notifier


class GridRunner:
    def __init__(self, cfg: Config, client, broker, store, notifier: Notifier):
        self.cfg = cfg
        self.client = client
        self.broker = broker
        self.store = store
        self.notifier = notifier

        g = cfg.raw.get("grid", {})
        self.symbol = cfg.symbol
        self.grids = int(g.get("grids", 10))
        self.quote_per_grid = float(g.get("quote_per_grid", 5))
        self.auto_range = bool(g.get("auto_range", True))
        self.range_pct = float(g.get("range_pct", 0.15))
        self.lower_cfg = g.get("lower")
        self.upper_cfg = g.get("upper")
        self.poll_seconds = int(g.get("poll_seconds", 20))
        self.breakout_buffer = float(g.get("breakout_buffer", 0.02))
        self.max_loss_quote = float(g.get("max_loss_quote", 0) or 0) or None
        self.reset_on_breakout = bool(g.get("reset_on_breakout", True))
        self._running = False

    # ----- 網格計算 -----
    def _levels(self, lower: float, upper: float) -> list[float]:
        step = (upper - lower) / self.grids
        return [lower + i * step for i in range(self.grids + 1)]

    def _new_grid(self, price: float) -> dict:
        if self.auto_range or not (self.lower_cfg and self.upper_cfg):
            lower = price * (1 - self.range_pct)
            upper = price * (1 + self.range_pct)
        else:
            lower, upper = float(self.lower_cfg), float(self.upper_cfg)
        state = {"active": True, "lower": lower, "upper": upper,
                 "grids": self.grids, "held": {}, "realized": 0.0,
                 "created_price": price}
        self.store.save_grid_state(state)
        self.notifier.send(
            f"🔲 開新網格：{lower:.2f}~{upper:.2f}（{self.grids} 格 / 每格 "
            f"{self.quote_per_grid} USDT）現價 {price:.2f}", "info", important=True)
        return state

    def _close_grid(self, state: dict, price: float, reason: str) -> None:
        held = {int(k): v for k, v in state.get("held", {}).items()}
        total_qty = sum(held.values())
        realized = state.get("realized", 0.0)
        if total_qty > 0:
            res = self.broker.market_sell(self.symbol, total_qty)
            if res.ok:
                self.store.record_trade(
                    symbol=self.symbol, side="SELL", base=res.filled_base,
                    quote=res.filled_quote, price=res.avg_price,
                    simulated=res.simulated, source="grid:close")
        state["active"] = False
        state["held"] = {}
        self.store.save_grid_state(state)
        self.notifier.send(
            f"⚠️ 關閉網格（{reason}）：賣出存貨 {total_qty:.8f} @ {price:.2f}"
            f"｜本輪累積已實現利潤 {realized:+.2f}", "info", important=True)

    # ----- 主迴圈 -----
    def run_once(self) -> None:
        try:
            price = self.broker.get_price(self.symbol)
        except Exception as exc:  # noqa: BLE001
            self.notifier.send(f"網格取價失敗：{exc}", "warning")
            return
        if not price or price <= 0:
            return

        state = self.store.load_grid_state()
        if not state or not state.get("active"):
            self._new_grid(price)
            return

        lower, upper = state["lower"], state["upper"]
        levels = self._levels(lower, upper)
        held = {int(k): v for k, v in state.get("held", {}).items()}

        # 風險檢查：跌破下緣 or 未實現虧損超限 → 關閉並重開
        unreal = sum(q * (price - levels[i]) for i, q in held.items())
        breach_down = price < lower * (1 - self.breakout_buffer)
        max_loss_hit = self.max_loss_quote is not None and unreal <= -self.max_loss_quote
        if breach_down or max_loss_hit:
            reason = "跌破下緣" if breach_down else f"未實現虧損達 {unreal:.2f}"
            self._close_grid(state, price, reason)
            if self.reset_on_breakout:
                self._new_grid(price)
            return

        # 突破上緣 → 獲利了結後向上重新定位
        if price > upper * (1 + self.breakout_buffer) and self.reset_on_breakout:
            self._close_grid(state, price, "突破上緣，向上重新定位")
            self._new_grid(price)
            return

        # 正常網格成交
        changed = False
        for i in range(self.grids):
            buy_px, sell_px = levels[i], levels[i + 1]
            if i in held and price >= sell_px:
                res = self.broker.market_sell(self.symbol, held[i])
                if res.ok:
                    profit = (res.avg_price - buy_px) * res.filled_base
                    state["realized"] = state.get("realized", 0.0) + profit
                    self.store.record_trade(
                        symbol=self.symbol, side="SELL", base=res.filled_base,
                        quote=res.filled_quote, price=res.avg_price,
                        simulated=res.simulated, source="grid", realized_pnl=profit)
                    del held[i]
                    changed = True
                    # 成交頻繁：只記 log、不外推通知
                    self.notifier.send(f"🟢 網格賣出 @ {res.avg_price:.2f}（+{profit:.2f}）",
                                       "info", push=False)
            if i not in held and price <= buy_px:
                res = self.broker.market_buy(self.symbol, self.quote_per_grid)
                if res.ok:
                    held[i] = res.filled_base
                    changed = True
                    self.store.record_trade(
                        symbol=self.symbol, side="BUY", base=res.filled_base,
                        quote=res.filled_quote, price=res.avg_price,
                        simulated=res.simulated, source="grid")
                    self.notifier.send(f"🔴 網格買入 @ {res.avg_price:.2f}",
                                       "info", push=False)

        if changed:
            state["held"] = {str(k): v for k, v in held.items()}
            self.store.save_grid_state(state)

    def run_forever(self) -> None:
        self._running = True
        mode = "實盤" if self.cfg.is_live else "紙上"
        need = self.quote_per_grid * self.grids
        self.notifier.send(
            f"🤖 網格機器人啟動（{mode}模式）｜{self.symbol}｜{self.grids} 格 / 每格 "
            f"{self.quote_per_grid} USDT（需約 {need:.0f} USDT）", "info", important=True)
        while self._running:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                self.notifier.send(f"⚠️ 網格迴圈錯誤：{exc}", "error")
            time.sleep(self.poll_seconds)

    def stop(self) -> None:
        self._running = False
