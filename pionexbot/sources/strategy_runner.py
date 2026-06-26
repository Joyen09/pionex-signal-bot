"""策略輪詢：定時抓 K 線、跑策略、把訊號交給 Executor。"""
from __future__ import annotations

import time

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

    def run_forever(self) -> None:
        self._running = True
        mode = "實盤" if self.cfg.is_live else "紙上"
        self.notifier.send(
            f"🤖 策略機器人啟動（{mode}模式）｜{self.symbol} {self.interval} "
            f"｜{self.strategy.name}｜每 {self.poll_seconds}s 評估一次", "info")
        while self._running:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - 單次錯誤不該讓機器人停止
                self.notifier.send(f"策略迴圈錯誤：{exc}", "error")
            time.sleep(self.poll_seconds)

    def stop(self) -> None:
        self._running = False
