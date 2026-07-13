"""多時間框架（MTF）對齊：防 lookahead 的核心。

規則：在 entry 時框第 i 根「收盤」的時點，高階時框只能看到
「收盤時間 <= entry 第 i 根收盤時間」的 K 線。
"""
from __future__ import annotations

from typing import Optional

from . import ohlc

_UNIT_MS = {"M": 60_000, "H": 3_600_000, "D": 86_400_000, "W": 604_800_000}


def interval_ms(interval: str) -> int:
    """'5M' / '60M' / '4H' / '1D' → 毫秒。"""
    s = str(interval).strip().upper()
    unit = s[-1]
    if unit not in _UNIT_MS:
        raise ValueError(f"無法解析週期：{interval!r}")
    return int(s[:-1]) * _UNIT_MS[unit]


def close_time(k, interval: str) -> Optional[float]:
    """K 線收盤時間（毫秒）。派網的 time 是開盤時間。"""
    t = ohlc.t(k)
    return None if t is None else float(t) + interval_ms(interval)


class MtfView:
    """把高階時框切片給 entry 迴圈用；單調前進，整體 O(n)。"""

    def __init__(self, klines, interval: str):
        self.klines = klines
        self.interval = interval
        self._ptr = 0

    def upto(self, entry_close_ms: float) -> list:
        """回傳收盤時間 <= entry_close_ms 的所有 K 線（防 lookahead）。"""
        while self._ptr < len(self.klines):
            ct = close_time(self.klines[self._ptr], self.interval)
            if ct is None or ct > entry_close_ms:
                break
            self._ptr += 1
        return self.klines[:self._ptr]
