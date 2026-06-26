"""均線交叉策略 (Moving Average Crossover)。

快線由下往上穿過慢線（黃金交叉）-> 買入
快線由上往下穿過慢線（死亡交叉）-> 賣出 / 平倉

只在「剛發生交叉」的那一根 K 線觸發，避免持續站上時不斷送出訊號。
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from ..models import Action, Signal
from .base import Strategy


class MaCrossStrategy(Strategy):
    name = "ma_cross"

    def __init__(self, params: dict[str, Any]):
        super().__init__(params)
        self.fast = int(params.get("fast", 9))
        self.slow = int(params.get("slow", 21))
        if self.fast >= self.slow:
            raise ValueError(f"fast({self.fast}) 必須小於 slow({self.slow})")

    def evaluate(self, klines: list[dict[str, Any]], symbol: str) -> Optional[Signal]:
        closes = self.closes(klines)
        if len(closes) < self.slow + 2:
            return None  # 資料不足

        s = pd.Series(closes)
        fast_ma = s.rolling(self.fast).mean()
        slow_ma = s.rolling(self.slow).mean()

        # 看最後兩根：前一根與當前的快慢線關係，判斷是否「剛交叉」
        prev_diff = fast_ma.iloc[-2] - slow_ma.iloc[-2]
        curr_diff = fast_ma.iloc[-1] - slow_ma.iloc[-1]
        if pd.isna(prev_diff) or pd.isna(curr_diff):
            return None

        price = closes[-1]
        if prev_diff <= 0 < curr_diff:
            return Signal(
                action=Action.BUY, symbol=symbol, source=f"strategy:{self.name}",
                price=price,
                reason=f"黃金交叉 MA{self.fast} 上穿 MA{self.slow}",
            )
        if prev_diff >= 0 > curr_diff:
            return Signal(
                action=Action.CLOSE, symbol=symbol, source=f"strategy:{self.name}",
                price=price,
                reason=f"死亡交叉 MA{self.fast} 下穿 MA{self.slow}",
            )
        return None
