"""內建策略。透過名稱建立策略物件。"""
from __future__ import annotations

from .base import Strategy
from .ma_cross import MaCrossStrategy

_REGISTRY = {
    "ma_cross": MaCrossStrategy,
}


def build_strategy(name: str, params: dict) -> Strategy:
    if name not in _REGISTRY:
        raise ValueError(f"未知策略 {name!r}，可用：{list(_REGISTRY)}")
    return _REGISTRY[name](params)
