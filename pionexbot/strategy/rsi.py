"""RSI 策略（均值回歸）。

RSI 由下往上穿過超賣線 -> 買入（跌深反彈）
RSI 由上往下穿過超買線 -> 賣出 / 平倉
只在「剛穿越」那一根觸發，避免連續送單。
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from ..models import Action, Signal
from . import indicators
from .base import Strategy


class RsiStrategy(Strategy):
    name = "rsi"

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        self.period = int(params.get("period", 14))
        self.oversold = float(params.get("oversold", 30))
        self.overbought = float(params.get("overbought", 70))

    def generate_signals(self, klines: list[dict[str, Any]]):
        closes = pd.Series(self.closes(klines))
        r = indicators.rsi(closes, self.period)
        prev = r.shift(1)
        actions: list[Optional[Action]] = [None] * len(closes)
        for i in range(len(closes)):
            p, c = prev.iloc[i], r.iloc[i]
            if pd.isna(p) or pd.isna(c):
                continue
            if p <= self.oversold < c:
                actions[i] = Action.BUY
            elif p >= self.overbought > c:
                actions[i] = Action.CLOSE
        return actions

    def evaluate(self, klines: list[dict[str, Any]], symbol: str) -> Optional[Signal]:
        closes = self.closes(klines)
        if len(closes) < self.period + 2:
            return None
        r = indicators.rsi(pd.Series(closes), self.period)
        prev, curr = r.iloc[-2], r.iloc[-1]
        if pd.isna(prev) or pd.isna(curr):
            return None
        price = closes[-1]
        if prev <= self.oversold < curr:
            return Signal(Action.BUY, symbol, source=f"strategy:{self.name}",
                          price=price,
                          reason=f"RSI 由 {prev:.1f} 上穿超賣線 {self.oversold}")
        if prev >= self.overbought > curr:
            return Signal(Action.CLOSE, symbol, source=f"strategy:{self.name}",
                          price=price,
                          reason=f"RSI 由 {prev:.1f} 下穿超買線 {self.overbought}")
        return None
