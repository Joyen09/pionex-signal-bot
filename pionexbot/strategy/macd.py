"""MACD 策略。

MACD 線上穿訊號線 -> 買入
MACD 線下穿訊號線 -> 賣出 / 平倉
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from ..models import Action, Signal
from . import indicators
from .base import Strategy


class MacdStrategy(Strategy):
    name = "macd"

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        self.fast = int(params.get("fast", 12))
        self.slow = int(params.get("slow", 26))
        self.signal = int(params.get("signal", 9))

    def evaluate(self, klines: list[dict[str, Any]], symbol: str) -> Optional[Signal]:
        closes = self.closes(klines)
        if len(closes) < self.slow + self.signal + 2:
            return None
        macd_line, signal_line, _ = indicators.macd(
            pd.Series(closes), self.fast, self.slow, self.signal)
        prev = macd_line.iloc[-2] - signal_line.iloc[-2]
        curr = macd_line.iloc[-1] - signal_line.iloc[-1]
        if pd.isna(prev) or pd.isna(curr):
            return None
        price = closes[-1]
        if prev <= 0 < curr:
            return Signal(Action.BUY, symbol, source=f"strategy:{self.name}",
                          price=price, reason="MACD 線上穿訊號線")
        if prev >= 0 > curr:
            return Signal(Action.CLOSE, symbol, source=f"strategy:{self.name}",
                          price=price, reason="MACD 線下穿訊號線")
        return None
