"""市場結構：swing point（分形）、趨勢狀態機、BOS / MSS。

防未來函數：
- swing 在右側 R 根收盤後才「確認」（confirmed_at = index + right）。
- 狀態機逐根推進，任何時點只看「當時已確認」的 swing；
  突破以收盤價判定，事件記在突破收盤的那根（confirmed_at）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import ohlc
from .types import Direction, StructureEvent, StructureKind, SwingKind, SwingPoint


def find_swings(klines, left: int = 3, right: int = 3) -> list[SwingPoint]:
    """分形 swing：high[i] 嚴格大於左 L、右 R 根內所有 high（等高不成立）。"""
    n = len(klines)
    highs = [ohlc.h(k) for k in klines]
    lows = [ohlc.l(k) for k in klines]
    out: list[SwingPoint] = []
    for i in range(left, n - right):
        neigh = range(i - left, i + right + 1)
        if all(highs[i] > highs[j] for j in neigh if j != i):
            out.append(SwingPoint(i, ohlc.t(klines[i]), highs[i],
                                  SwingKind.HIGH, confirmed_at=i + right))
        if all(lows[i] < lows[j] for j in neigh if j != i):
            out.append(SwingPoint(i, ohlc.t(klines[i]), lows[i],
                                  SwingKind.LOW, confirmed_at=i + right))
    return out


@dataclass
class StructureState:
    """狀態機輸出：事件列表 + 每個時點的趨勢（給策略／繪圖用）。"""

    events: list[StructureEvent] = field(default_factory=list)
    trend: list[Optional[Direction]] = field(default_factory=list)  # 每根 K 的趨勢
    # UP 趨勢的「成本區」：造成最近一次向上 BOS 的 higher-low（MSS 觸發線）
    protective_low: Optional[SwingPoint] = None
    protective_high: Optional[SwingPoint] = None


def detect_structure(klines, swings: Optional[list[SwingPoint]] = None,
                     left: int = 3, right: int = 3,
                     break_mode: str = "close") -> StructureState:
    """逐根走一遍 K 線，輸出 BOS / MSS 事件與趨勢序列。

    - BOS：順勢收盤突破「最近一個已確認、且尚未被突破」的 swing。
    - MSS：收盤跌破本段趨勢的成本區（protective swing）→ 趨勢翻轉。
    - UNDEFINED 起始：第一次突破任一已確認 swing 即定調（記為 BOS）。
    """
    swings = swings if swings is not None else find_swings(klines, left, right)
    st = StructureState()
    trend: Optional[Direction] = None

    # 依確認時間排序，逐根餵給狀態機
    by_confirm = sorted(swings, key=lambda s: s.confirmed_at)
    si = 0
    last_high: Optional[SwingPoint] = None   # 最近已確認且未被突破的 swing high
    last_low: Optional[SwingPoint] = None
    # 突破後用來當 protective 的「最近確認 swing」快照
    recent_low: Optional[SwingPoint] = None  # 最近已確認 swing low（不論是否被破）
    recent_high: Optional[SwingPoint] = None

    for i, k in enumerate(klines):
        # 1) 收進本根確認的 swing
        while si < len(by_confirm) and by_confirm[si].confirmed_at <= i:
            s = by_confirm[si]
            if s.kind == SwingKind.HIGH:
                last_high = s
                recent_high = s
            else:
                last_low = s
                recent_low = s
            si += 1

        px = ohlc.c(k) if break_mode == "close" else None
        up_px = px if px is not None else ohlc.h(k)
        dn_px = px if px is not None else ohlc.l(k)

        # 2) MSS 優先：收盤跌破成本區 → 趨勢翻轉
        if trend == Direction.UP and st.protective_low is not None \
                and dn_px < st.protective_low.price:
            st.events.append(StructureEvent(
                StructureKind.MSS, Direction.DOWN, st.protective_low.price,
                st.protective_low, confirmed_at=i, time=ohlc.t(k)))
            trend = Direction.DOWN
            st.protective_low = None
            st.protective_high = recent_high
            last_low = None if last_low and dn_px < last_low.price else last_low
        elif trend == Direction.DOWN and st.protective_high is not None \
                and up_px > st.protective_high.price:
            st.events.append(StructureEvent(
                StructureKind.MSS, Direction.UP, st.protective_high.price,
                st.protective_high, confirmed_at=i, time=ohlc.t(k)))
            trend = Direction.UP
            st.protective_high = None
            st.protective_low = recent_low
            last_high = None if last_high and up_px > last_high.price else last_high

        # 3) BOS：順勢（或未定調時首破定調）
        if last_high is not None and up_px > last_high.price \
                and trend in (None, Direction.UP):
            st.events.append(StructureEvent(
                StructureKind.BOS, Direction.UP, last_high.price,
                last_high, confirmed_at=i, time=ohlc.t(k)))
            trend = Direction.UP
            st.protective_low = recent_low   # 造成這次 BOS 的 higher-low
            last_high = None                 # 破掉的不再重複觸發
        elif last_low is not None and dn_px < last_low.price \
                and trend in (None, Direction.DOWN):
            st.events.append(StructureEvent(
                StructureKind.BOS, Direction.DOWN, last_low.price,
                last_low, confirmed_at=i, time=ohlc.t(k)))
            trend = Direction.DOWN
            st.protective_high = recent_high
            last_low = None

        st.trend.append(trend)
    return st
