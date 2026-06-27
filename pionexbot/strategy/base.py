"""策略基底類別。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from ..models import Signal


class Strategy(ABC):
    name = "base"

    def __init__(self, params: dict[str, Any]):
        self.params = params

    @abstractmethod
    def evaluate(self, klines: list[dict[str, Any]], symbol: str) -> Optional[Signal]:
        """輸入 K 線（舊到新），回傳交易訊號或 None（不動作）。"""
        raise NotImplementedError

    def generate_signals(self, klines: list[dict[str, Any]]):
        """一次算完整段每根的動作（向量化用於回測）。

        回傳長度同 klines 的 list，元素為 Action 或 None。
        預設為逐根呼叫 evaluate（慢，O(n^2)）；各策略應覆寫為向量化版本。
        """
        out = []
        for i in range(len(klines)):
            sig = self.evaluate(klines[: i + 1], "")
            out.append(sig.action if sig else None)
        return out

    @staticmethod
    def closes(klines: list[dict[str, Any]]) -> list[float]:
        """從 K 線抽出收盤價，容忍不同欄位命名。"""
        out: list[float] = []
        for k in klines:
            if isinstance(k, dict):
                v = k.get("close", k.get("c"))
            else:  # 有時 API 回傳 list 格式 [time, open, high, low, close, volume]
                v = k[4]
            out.append(float(v))
        return out
