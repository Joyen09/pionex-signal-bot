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


def _atr_series(klines, period: int = 14) -> list[float]:
    """逐根 ATR（簡單平均，根數不足時用現有均值）。只看過去，無未來函數。"""
    atrs: list[float] = []
    trs: list[float] = []
    prev_close: float | None = None
    for k in klines:
        hi, lo, cl = ohlc.h(k), ohlc.l(k), ohlc.c(k)
        tr = hi - lo if prev_close is None else max(
            hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        window = trs[-period:]
        atrs.append(sum(window) / len(window))
        prev_close = cl
    return atrs


def _has_fvg_in(klines, start: int, end: int, direction: Direction,
                min_size_pct: float) -> bool:
    """[start, end] 推進段內是否含至少一個「事件同向」的合格 FVG。"""
    for i in range(max(start + 2, 2), min(end, len(klines) - 1) + 1):
        c1, c3 = klines[i - 2], klines[i]
        px = ohlc.c(c3)
        if px <= 0:
            continue
        if direction == Direction.UP and ohlc.l(c3) > ohlc.h(c1):
            top, bottom = ohlc.l(c3), ohlc.h(c1)
        elif direction == Direction.DOWN and ohlc.h(c3) < ohlc.l(c1):
            top, bottom = ohlc.l(c1), ohlc.h(c3)
        else:
            continue
        if (top - bottom) / px >= min_size_pct:
            return True
    return False


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
                     break_mode: str = "close",
                     smc_cfg: Optional[dict] = None) -> StructureState:
    """逐根走一遍 K 線，輸出 BOS / MSS 事件與趨勢序列。

    - BOS：順勢收盤突破「最近一個已確認、且尚未被突破」的 swing。
    - MSS：收盤跌破本段趨勢的成本區（protective swing）→ 趨勢翻轉。
    - UNDEFINED 起始：第一次突破任一已確認 swing 即定調（記為 BOS）。
    - displacement（v1.1）：每個事件標注位移品質（smc.structure 設定），
      狀態機本身不受影響——弱勢突破照樣翻面，只是事件被標為弱。
    """
    swings = swings if swings is not None else find_swings(klines, left, right)
    sc = smc_cfg or {}
    st_cfg = sc.get("structure", {})
    disp_mode = str(st_cfg.get("displacement_mode", "either"))
    disp_atr = float(st_cfg.get("min_displacement_atr", 0.5))
    fvg_min = float(sc.get("fvg", {}).get("min_size_pct", 0.0005))
    atrs = _atr_series(klines)

    def _displaced(i: int, direction: Direction,
                   seg_start: Optional[int]) -> bool:
        k = klines[i]
        body_ok = abs(ohlc.c(k) - ohlc.o(k)) >= disp_atr * atrs[i] \
            if atrs[i] > 0 else True
        if disp_mode == "atr":
            return body_ok
        start = seg_start if seg_start is not None else max(0, i - 10)
        fvg_ok = _has_fvg_in(klines, start, i, direction, fvg_min)
        if disp_mode == "fvg":
            return fvg_ok
        return body_ok or fvg_ok      # either

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
                st.protective_low, confirmed_at=i, time=ohlc.t(k),
                displacement=_displaced(
                    i, Direction.DOWN,
                    recent_high.index if recent_high else None)))
            trend = Direction.DOWN
            st.protective_low = None
            st.protective_high = recent_high
            last_low = None if last_low and dn_px < last_low.price else last_low
        elif trend == Direction.DOWN and st.protective_high is not None \
                and up_px > st.protective_high.price:
            st.events.append(StructureEvent(
                StructureKind.MSS, Direction.UP, st.protective_high.price,
                st.protective_high, confirmed_at=i, time=ohlc.t(k),
                displacement=_displaced(
                    i, Direction.UP,
                    recent_low.index if recent_low else None)))
            trend = Direction.UP
            st.protective_high = None
            st.protective_low = recent_low
            last_high = None if last_high and up_px > last_high.price else last_high

        # 3) BOS：順勢（或未定調時首破定調）
        if last_high is not None and up_px > last_high.price \
                and trend in (None, Direction.UP):
            st.events.append(StructureEvent(
                StructureKind.BOS, Direction.UP, last_high.price,
                last_high, confirmed_at=i, time=ohlc.t(k),
                displacement=_displaced(
                    i, Direction.UP,
                    recent_low.index if recent_low else None)))
            trend = Direction.UP
            st.protective_low = recent_low   # 造成這次 BOS 的 higher-low
            last_high = None                 # 破掉的不再重複觸發
        elif last_low is not None and dn_px < last_low.price \
                and trend in (None, Direction.DOWN):
            st.events.append(StructureEvent(
                StructureKind.BOS, Direction.DOWN, last_low.price,
                last_low, confirmed_at=i, time=ohlc.t(k),
                displacement=_displaced(
                    i, Direction.DOWN,
                    recent_high.index if recent_high else None)))
            trend = Direction.DOWN
            st.protective_high = recent_high
            last_low = None

        st.trend.append(trend)
    return st
