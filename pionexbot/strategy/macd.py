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

    def generate_signals(self, klines: list[dict[str, Any]]):
        closes = pd.Series(self.closes(klines))
        macd_line, signal_line, _ = indicators.macd(
            closes, self.fast, self.slow, self.signal)
        diff = macd_line - signal_line
        prev = diff.shift(1)
        actions: list[Optional[Action]] = [None] * len(closes)
        for i in range(len(closes)):
            p, c = prev.iloc[i], diff.iloc[i]
            if pd.isna(p) or pd.isna(c):
                continue
            if p <= 0 < c:
                actions[i] = Action.BUY
            elif p >= 0 > c:
                actions[i] = Action.CLOSE
        return actions

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
