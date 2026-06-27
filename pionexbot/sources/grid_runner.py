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
from datetime import datetime, timezone

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

        # 每日結算（utc_hour 預設 12 = 台灣晚上 8 點）
        dcfg = cfg.notify.get("daily_summary", {})
        self.summary_enabled = bool(dcfg.get("enabled", True))
        self.summary_hour = int(dcfg.get("utc_hour", 12))
        self._last_summary_day = store.get_meta("grid_summary_day", "") or ""

        # Telegram 指令輪詢用的 offset（記在 store，重啟不重播舊訊息）
        off = store.get_meta("tg_offset", None)
        self._tg_offset = int(off) if off is not None else None

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
                 "created_price": price, "last_price": price}
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

    # ----- 狀態查詢 / 通知互動 -----
    def status_text(self) -> str:
        state = self.store.load_grid_state()
        if not state or not state.get("active"):
            return "目前沒有運行中的網格。"
        lower, upper = state["lower"], state["upper"]
        levels = self._levels(lower, upper)
        held = {int(k): v for k, v in state.get("held", {}).items()}
        try:
            price = self.broker.get_price(self.symbol)
        except Exception:  # noqa: BLE001
            price = 0.0
        invested = sum(q * levels[i] for i, q in held.items())
        unreal = sum(q * (price - levels[i]) for i, q in held.items()) if price else 0.0
        lines = [
            f"🔲 網格狀態（{self.symbol}）",
            f"區間 {lower:.0f}~{upper:.0f}，共 {self.grids} 格",
            f"現價 {price:.2f}",
            f"持有 {len(held)} 格：",
        ]
        for i in sorted(held):
            lines.append(f"  ・買在 {levels[i]:.0f}（{held[i]:.8f}）")
        if not held:
            lines.append("  （目前空手）")
        lines.append(f"投入成本 {invested:.2f} USDT，未實現 {unreal:+.2f}")
        lines.append(f"本網格已實現利潤 {state.get('realized', 0.0):+.2f}")
        return "\n".join(lines)

    def _poll_commands(self) -> None:
        """讀取 Telegram 訊息：收到任何訊息就回覆網格狀態。"""
        if not self.notifier.tg_enabled:
            return
        updates = self.notifier.get_updates(offset=self._tg_offset)
        if not updates:
            return
        first_run = self._tg_offset is None
        for u in updates:
            self._tg_offset = int(u["update_id"]) + 1
            if first_run:
                continue  # 首次啟動只記位置，不回應啟動前的舊訊息
            msg = u.get("message") or u.get("channel_post") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            if not msg.get("text"):
                continue
            # 只回應設定的 chat_id（避免陌生人查你的部位）
            if self.notifier.tg_chat and chat != str(self.notifier.tg_chat):
                continue
            self.notifier.send(self.status_text(), important=True)
        self.store.set_meta("tg_offset", self._tg_offset)

    def _maybe_daily_summary(self) -> None:
        if not self.summary_enabled:
            return
        now = datetime.now(timezone.utc)
        if now.hour < self.summary_hour:
            return
        today = now.strftime("%Y-%m-%d")
        if self._last_summary_day == today:
            return
        self._last_summary_day = today
        self.store.set_meta("grid_summary_day", today)
        self.notifier.send("📊 每日結算\n" + self.status_text(), important=True)

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
        last = state.get("last_price", price)   # 上一輪價格，用來判斷「穿越」

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

        # 正常網格成交：只在價格「穿越」某格線時才成交那一格
        for i in range(self.grids):
            buy_px, sell_px = levels[i], levels[i + 1]
            # 賣出：持有，且價格由下『向上穿過』上一格
            if i in held and last < sell_px <= price:
                res = self.broker.market_sell(self.symbol, held[i])
                if res.ok:
                    profit = (res.avg_price - buy_px) * res.filled_base
                    state["realized"] = state.get("realized", 0.0) + profit
                    self.store.record_trade(
                        symbol=self.symbol, side="SELL", base=res.filled_base,
                        quote=res.filled_quote, price=res.avg_price,
                        simulated=res.simulated, source="grid", realized_pnl=profit)
                    del held[i]
                    # 成交頻繁：只記 log、不外推通知
                    self.notifier.send(f"🟢 網格賣出 @ {res.avg_price:.2f}（+{profit:.2f}）",
                                       "info", push=False)
            # 買入：未持有，且價格由上『向下穿過』該格
            if i not in held and last > buy_px >= price:
                res = self.broker.market_buy(self.symbol, self.quote_per_grid)
                if res.ok:
                    held[i] = res.filled_base
                    self.store.record_trade(
                        symbol=self.symbol, side="BUY", base=res.filled_base,
                        quote=res.filled_quote, price=res.avg_price,
                        simulated=res.simulated, source="grid")
                    self.notifier.send(f"🔴 網格買入 @ {res.avg_price:.2f}",
                                       "info", push=False)

        state["held"] = {str(k): v for k, v in held.items()}
        state["last_price"] = price
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
                self._poll_commands()       # 回覆 Telegram 查詢
                self.run_once()             # 網格交易
                self._maybe_daily_summary() # 每日結算
            except Exception as exc:  # noqa: BLE001
                self.notifier.send(f"⚠️ 網格迴圈錯誤：{exc}", "error")
            time.sleep(self.poll_seconds)

    def stop(self) -> None:
        self._running = False
