"""策略輪詢：定時抓 K 線、跑策略、把訊號交給 Executor。"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from ..config import Config
from ..executor import Executor
from ..notifier import Notifier
from ..pionex_client import PionexClient, PionexError
from ..strategy import build_strategy


class StrategyRunner:
    def __init__(self, cfg: Config, client: PionexClient, executor: Executor,
                 notifier: Notifier):
        self.cfg = cfg
        self.client = client
        self.executor = executor
        self.notifier = notifier

        scfg = cfg.strategy
        self.symbol = cfg.symbol
        self.interval = scfg.get("interval", "5M")
        self.poll_seconds = int(scfg.get("poll_seconds", 30))
        self.strategy = build_strategy(scfg.get("name", "ma_cross"),
                                       scfg.get("params", {}))
        self._running = False

        # 每日損益摘要設定（utc_hour 預設 1 = 台灣時間 09:00）
        dcfg = cfg.notify.get("daily_summary", {})
        self.summary_enabled = bool(dcfg.get("enabled", False))
        self.summary_utc_hour = int(dcfg.get("utc_hour", 1))
        self._last_summary_day = ""

    def run_once(self) -> None:
        try:
            klines = self.client.get_klines(self.symbol, self.interval, limit=200)
        except PionexError as exc:
            self.notifier.send(f"抓 K 線失敗：{exc}", "warning")
            return
        signal = self.strategy.evaluate(klines, self.symbol)
        if signal is None:
            return
        # 買入訊號補上設定的下單金額
        if signal.quote_amount is None and signal.base_size is None:
            signal.quote_amount = float(self.cfg.trading.get("quote_per_trade", 20))
        self.notifier.send(f"📈 策略訊號：{signal}", "info")
        self.executor.handle(signal)

    def _maybe_daily_summary(self) -> None:
        """到了設定的時間就推一次每日摘要（每天最多一次）。"""
        if not self.summary_enabled:
            return
        now = datetime.now(timezone.utc)
        if now.hour < self.summary_utc_hour:
            return
        today = now.strftime("%Y-%m-%d")
        if self._last_summary_day == today:
            return
        self._last_summary_day = today
        self._send_summary(now)

    def _send_summary(self, now: datetime) -> None:
        store = self.executor.store
        pos = store.load_position()
        count, realized = store.stats_since(now.timestamp() - 86400)
        try:
            price = self.executor.broker.get_price(self.symbol)
        except Exception:  # noqa: BLE001 - 摘要取不到價不該中斷
            price = 0.0
        unrealized = (price - pos.avg_cost) * pos.base if (pos.base > 0 and price) else 0.0
        mode = "實盤" if self.cfg.is_live else "紙上"
        msg = "\n".join([
            f"📊 每日摘要 {now.strftime('%Y-%m-%d')}（{mode}）",
            f"交易對：{self.symbol}",
            f"過去 24h：成交 {count} 筆，已實現損益 {realized:+.2f}",
            f"目前持倉：{pos.base:.8f} @ 均價 {pos.avg_cost:.2f}",
            f"現價 {price:.2f}，未實現損益 {unrealized:+.2f}",
        ])
        self.notifier.send(msg, "info", important=True)

    def run_forever(self) -> None:
        self._running = True
        mode = "實盤" if self.cfg.is_live else "紙上"
        # 啟動通知（important → 會推 LINE/Telegram）
        self.notifier.send(
            f"🤖 策略機器人啟動（{mode}模式）｜{self.symbol} {self.interval} "
            f"｜{self.strategy.name}｜每 {self.poll_seconds}s 評估一次",
            "info", important=True)
        while self._running:
            try:
                self.run_once()
                self._maybe_daily_summary()
            except Exception as exc:  # noqa: BLE001 - 單次錯誤不該讓機器人停止
                self.notifier.send(f"⚠️ 策略迴圈錯誤：{exc}", "error")
            time.sleep(self.poll_seconds)

    def stop(self) -> None:
        self._running = False
