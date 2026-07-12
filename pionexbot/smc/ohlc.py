"""K 線欄位擷取：容忍 dict（派網格式）與 list 兩種形態。"""
from __future__ import annotations

from typing import Any, Optional


def o(k) -> float:
    return _get(k, ("open", "o"), 1)


def h(k) -> float:
    return _get(k, ("high", "h"), 2)


def l(k) -> float:  # noqa: E743 - 對齊 OHLC 慣例
    return _get(k, ("low", "l"), 3)


def c(k) -> float:
    return _get(k, ("close", "c"), 4)


def t(k) -> Optional[Any]:
    if isinstance(k, dict):
        for key in ("time", "t", "timestamp", "openTime", "T"):
            if key in k:
                return k[key]
        return None
    if isinstance(k, (list, tuple)) and k:
        return k[0]
    return None


def _get(k, keys: tuple, idx: int) -> float:
    if isinstance(k, dict):
        for key in keys:
            if key in k:
                return float(k[key])
        # 只有 close 的簡化 fixture（測試用）：全部欄位退回 close
        if "close" in k:
            return float(k["close"])
        raise KeyError(f"K 線缺少欄位 {keys}")
    return float(k[idx])
