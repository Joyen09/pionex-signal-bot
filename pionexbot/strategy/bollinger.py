"""布林通道策略（均值回歸）。

收盤價跌破下軌 -> 買入（預期回到均值）
收盤價突破上軌 -> 賣出 / 平倉
只在「剛跌破/突破」那一根觸發。
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from ..models import Action, Signal
from . import indicators
from .base import Strategy


class BollingerStrategy(Strategy):
    name = "bollinger"

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        self.period = int(params.get("period", 20))
        self.num_std = float(params.get("num_std", 2.0))

    def generate_signals(self, klines: list[dict[str, Any]]):
        closes = pd.Series(self.closes(klines))
        _, upper, lower = indicators.bollinger(closes, self.period, self.num_std)
        prev_c = closes.shift(1)
        prev_l, prev_u = lower.shift(1), upper.shift(1)
        actions: list[Optional[Action]] = [None] * len(closes)
        for i in range(len(closes)):
            if pd.isna(lower.iloc[i]) or pd.isna(upper.iloc[i]) or pd.isna(prev_c.iloc[i]):
                continue
            if prev_c.iloc[i] >= prev_l.iloc[i] and closes.iloc[i] < lower.iloc[i]:
                actions[i] = Action.BUY
            elif prev_c.iloc[i] <= prev_u.iloc[i] and closes.iloc[i] > upper.iloc[i]:
                actions[i] = Action.CLOSE
        return actions

    def evaluate(self, klines: list[dict[str, Any]], symbol: str) -> Optional[Signal]:
        closes = self.closes(klines)
        if len(closes) < self.period + 2:
            return None
        _, upper, lower = indicators.bollinger(
            pd.Series(closes), self.period, self.num_std)
        prev_c, curr_c = closes[-2], closes[-1]
        if pd.isna(lower.iloc[-1]) or pd.isna(upper.iloc[-1]):
            return None
        price = curr_c
        # 由上往下跌破下軌
        if prev_c >= lower.iloc[-2] and curr_c < lower.iloc[-1]:
            return Signal(Action.BUY, symbol, source=f"strategy:{self.name}",
                          price=price, reason=f"跌破下軌 {lower.iloc[-1]:.2f}")
        # 由下往上突破上軌
        if prev_c <= upper.iloc[-2] and curr_c > upper.iloc[-1]:
            return Signal(Action.CLOSE, symbol, source=f"strategy:{self.name}",
                          price=price, reason=f"突破上軌 {upper.iloc[-1]:.2f}")
        return None
