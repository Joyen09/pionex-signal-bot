"""內建策略。透過名稱建立策略物件。"""
from __future__ import annotations

from .base import Strategy
from .bollinger import BollingerStrategy
from .ma_cross import MaCrossStrategy
from .macd import MacdStrategy
from .rsi import RsiStrategy

_REGISTRY: dict[str, type[Strategy]] = {
    "ma_cross": MaCrossStrategy,
    "rsi": RsiStrategy,
    "macd": MacdStrategy,
    "bollinger": BollingerStrategy,
}


def available() -> list[str]:
    return list(_REGISTRY)


def build_strategy(name: str, params: dict) -> Strategy:
    if name not in _REGISTRY:
        raise ValueError(f"未知策略 {name!r}，可用：{available()}")
    return _REGISTRY[name](params)
