"""智慧 DCA 執行器：定期定額 + 逢低加碼，只買不賣（長期累積）。

- 每隔 interval_hours 買一次。
- 參考均線（近 ref_period 根 indicator_interval K 線）。價格低於均線越多 → 買越多。
- Telegram：傳訊息回覆狀態；每日結算。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from ..config import Config
from ..dca import smart_amount


class DcaRunner:
    def __init__(self, cfg: Config, client, broker, store, notifier):
        self.cfg = cfg
        self.client = client
        self.broker = broker
        self.store = store
        self.notifier = notifier

        d = cfg.raw.get("dca", {})
        self.symbol = cfg.symbol
        self.quote_base = float(d.get("quote_base", 5))
        self.interval_hours = float(d.get("interval_hours", 24))
        self.indicator_interval = str(d.get("indicator_interval", "1D"))
        self.ref_period = int(d.get("ref_period", 20))
        self.dip_step = float(d.get("dip_step", 0.05))
        self.mult_per_step = float(d.get("mult_per_step", 0.5))
        self.max_mult = float(d.get("max_mult", 3.0))
        self.poll_seconds = int(d.get("poll_seconds", 30))
        # 停利：帳面獲利達此 % 就賣出落袋（0=關閉）；賣出比例 1.0=全賣後重新定投
        self.take_profit_pct = float(d.get("take_profit_pct", 0) or 0)
        self.tp_fraction = min(max(float(d.get("take_profit_fraction", 1.0)), 0.0), 1.0)

        dc = cfg.notify.get("daily_summary", {})
        self.summary_enabled = bool(dc.get("enabled", True))
        self.summary_hour = int(dc.get("utc_hour", 12))
        self._last_summary_day = store.get_meta("dca_summary_day", "") or ""

        self._tg_offset = None
        self._start_ts = time.time()
        self._running = False

    # ----- 參考均線 -----
    def _reference(self) -> float | None:
        if self.client is None:
            return None
        try:
            kl = self.client.get_klines(self.symbol, self.indicator_interval,
                                        limit=self.ref_period + 2)
        except Exception:  # noqa: BLE001
            return None
        closes = []
        for k in kl:
            v = k.get("close", k.get("c")) if isinstance(k, dict) else k[4]
            closes.append(float(v))
        if len(closes) < self.ref_period:
            return None
        return sum(closes[-self.ref_period:]) / self.ref_period

    # ----- 狀態 -----
    def status_text(self) -> str:
        st = self.store.load_dca_state() or {}
        invested = st.get("total_quote", 0.0)
        base = st.get("total_base", 0.0)
        try:
            price = self.broker.get_price(self.symbol)
        except Exception:  # noqa: BLE001
            price = 0.0
        value = base * price
        avg = invested / base if base else 0.0
        pnl = value - invested
        pct = (pnl / invested * 100) if invested else 0.0
        realized = st.get("realized_total", 0.0)
        lines = [
            f"🟣 DCA 狀態（{self.symbol}）",
            f"本輪投入 {invested:.2f} USDT",
            f"持有 {base:.8f}，均價 {avg:.2f}",
            f"現價 {price:.2f}，現值 {value:.2f}",
            f"未實現 {pnl:+.2f}（{pct:+.1f}%）",
            f"買入次數 {st.get('buys', 0)}",
        ]
        if realized:
            lines.append(f"累積已實現停利 {realized:+.2f}")
        return "\n".join(lines)

    def _poll_commands(self) -> None:
        if not self.notifier.tg_enabled:
            return
        for u in self.notifier.get_updates(offset=self._tg_offset):
            self._tg_offset = int(u["update_id"]) + 1
            msg = u.get("message") or {}
            if not msg.get("text") or float(msg.get("date", 0)) < self._start_ts:
                continue
            chat = str((msg.get("chat") or {}).get("id", ""))
            if self.notifier.tg_chat and chat != str(self.notifier.tg_chat):
                continue
            self.notifier.send(self.status_text(), important=True)

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
        self.store.set_meta("dca_summary_day", today)
        self.notifier.send("📊 每日結算\n" + self.status_text(), important=True)

    # ----- 買入 -----
    def _maybe_buy(self) -> None:
        st = self.store.load_dca_state() or {"total_quote": 0.0, "total_base": 0.0,
                                             "buys": 0, "last_buy_ts": 0.0}
        now = time.time()
        if st.get("last_buy_ts", 0) and now - st["last_buy_ts"] < self.interval_hours * 3600:
            return  # 還沒到下次買入時間

        try:
            price = self.broker.get_price(self.symbol)
        except Exception as exc:  # noqa: BLE001
            self.notifier.send(f"DCA 取價失敗：{exc}", "warning")
            return
        if not price or price <= 0:
            return

        ref = self._reference()
        amount, mult = smart_amount(price, ref, self.quote_base,
                                    self.dip_step, self.mult_per_step, self.max_mult)
        res = self.broker.market_buy(self.symbol, amount)
        if not res.ok:
            self.notifier.send(f"❌ DCA 買入失敗：{res.error}", "error")
            return
        st["total_quote"] += res.filled_quote
        st["total_base"] += res.filled_base
        st["buys"] += 1
        st["last_buy_ts"] = now
        self.store.save_dca_state(st)
        self.store.record_trade(symbol=self.symbol, side="BUY", base=res.filled_base,
                                quote=res.filled_quote, price=res.avg_price,
                                simulated=res.simulated, source="dca")
        tag = f"（逢低 {mult:.1f}×）" if mult > 1 else ""
        self.notifier.send(
            f"🟣 DCA 買入 {res.filled_quote:.2f} USDT{tag} @ {res.avg_price:.2f}",
            "info", important=True)

    def _maybe_take_profit(self) -> None:
        """帳面獲利達標 → 賣出落袋；全賣則重新開始定投。"""
        if not self.take_profit_pct:
            return
        st = self.store.load_dca_state()
        if not st:
            return
        base = st.get("total_base", 0.0)
        invested = st.get("total_quote", 0.0)
        if base <= 0 or invested <= 0:
            return
        try:
            price = self.broker.get_price(self.symbol)
        except Exception:  # noqa: BLE001
            return
        ret = (base * price - invested) / invested
        if ret < self.take_profit_pct:
            return

        sell_base = base * self.tp_fraction
        res = self.broker.market_sell(self.symbol, sell_base)
        if not res.ok:
            self.notifier.send(f"❌ DCA 停利賣出失敗：{res.error}", "error")
            return
        cost_sold = invested * self.tp_fraction
        realized = res.filled_quote - cost_sold
        st["realized_total"] = st.get("realized_total", 0.0) + realized
        st["total_base"] = base - res.filled_base
        st["total_quote"] = invested - cost_sold
        if st["total_base"] <= 1e-12 or self.tp_fraction >= 1.0:
            st["total_base"] = 0.0
            st["total_quote"] = 0.0
            st["buys"] = 0           # 全賣 → 重新開始定投
            st["last_buy_ts"] = 0.0
        self.store.save_dca_state(st)
        self.store.record_trade(symbol=self.symbol, side="SELL", base=res.filled_base,
                                quote=res.filled_quote, price=res.avg_price,
                                simulated=res.simulated, source="dca:tp",
                                realized_pnl=realized)
        again = "，重新開始定投" if self.tp_fraction >= 1.0 else "，剩餘續抱"
        self.notifier.send(
            f"🎯 DCA 停利 +{ret*100:.1f}%！賣出 {res.filled_quote:.2f} USDT"
            f"（落袋 {realized:+.2f}）{again}", "info", important=True)

    def run_forever(self) -> None:
        self._running = True
        mode = "實盤" if self.cfg.is_live else "紙上"
        every = ("每天" if self.interval_hours == 24 else
                 "每週" if self.interval_hours == 168 else f"每 {self.interval_hours:.0f} 小時")
        self.notifier.send(
            f"🤖 DCA 機器人啟動（{mode}模式）｜{self.symbol}｜{every}買 "
            f"{self.quote_base} USDT（逢低最高 {self.max_mult:.0f}×）", "info", important=True)
        while self._running:
            try:
                self._poll_commands()
                self._maybe_take_profit()   # 先檢查停利
                self._maybe_buy()
                self._maybe_daily_summary()
            except Exception as exc:  # noqa: BLE001
                self.notifier.send(f"⚠️ DCA 迴圈錯誤：{exc}", "error")
            time.sleep(self.poll_seconds)

    def stop(self) -> None:
        self._running = False
