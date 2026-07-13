"""進場區域：OB / PB / Breaker、FVG / IFVG / BPR、Premium-Discount / OTE。

狀態機（隨後續 K 線推進，時間記在轉變當根）：
    FRESH →（價格觸及）TESTED →（收盤穿越遠端）FLIPPED（OB→Breaker、FVG→IFVG）
    影線走完整個區域但未收盤穿越 → FILLED
"""
from __future__ import annotations

from typing import Optional

from . import ohlc
from .types import (Direction, StructureEvent, Zone, ZoneKind, ZoneState)


# ------------------------------------------------------------------ #
# FVG
# ------------------------------------------------------------------ #
def detect_fvgs(klines, min_size_pct: float = 0.0005) -> list[Zone]:
    """三根 K 的失衡缺口。多頭：low(c3) > high(c1)，區域 [high(c1), low(c3)]。"""
    zones: list[Zone] = []
    for i in range(2, len(klines)):
        c1, c3 = klines[i - 2], klines[i]
        px = ohlc.c(c3)
        if px <= 0:
            continue
        if ohlc.l(c3) > ohlc.h(c1):        # 多頭 FVG
            top, bottom = ohlc.l(c3), ohlc.h(c1)
            direction = Direction.UP
        elif ohlc.h(c3) < ohlc.l(c1):      # 空頭 FVG
            top, bottom = ohlc.l(c1), ohlc.h(c3)
            direction = Direction.DOWN
        else:
            continue
        if (top - bottom) / px < min_size_pct:
            continue
        zones.append(Zone(ZoneKind.FVG, top, bottom, direction,
                          created_at=i, time=ohlc.t(klines[i])))
    _advance_states(zones, klines)
    return zones


# ------------------------------------------------------------------ #
# OB / PB
# ------------------------------------------------------------------ #
def detect_obs(klines, events: list[StructureEvent],
               zone_mode: str = "full_range") -> list[Zone]:
    """每次 BOS/MSS 確認時回溯：推進段中最後一根「反向實體」K 即訂單塊。

    向上突破 → 找突破根之前最後一根收黑（close < open）的 K。
    PB：趨勢延續（BOS 且前一事件同向）中生成的同向 OB。
    """
    zones: list[Zone] = []
    prev_dir: Optional[Direction] = None
    for ev in events:
        lo_bound = ev.swing_ref.index  # 推進段起點（保守用被破 swing 的位置）
        found = None
        for j in range(ev.confirmed_at - 1, max(lo_bound - 1, -1), -1):
            k = klines[j]
            body_bear = ohlc.c(k) < ohlc.o(k)
            if (ev.direction == Direction.UP and body_bear) or \
                    (ev.direction == Direction.DOWN and not body_bear
                     and ohlc.c(k) > ohlc.o(k)):
                found = j
                break
        if found is not None:
            k = klines[found]
            if zone_mode == "body":
                top = max(ohlc.o(k), ohlc.c(k))
                bottom = min(ohlc.o(k), ohlc.c(k))
            else:
                top, bottom = ohlc.h(k), ohlc.l(k)
            from .types import StructureKind
            is_continuation = (ev.kind == StructureKind.BOS
                               and prev_dir == ev.direction)
            zones.append(Zone(
                ZoneKind.PB if is_continuation else ZoneKind.OB,
                top, bottom, ev.direction,
                created_at=ev.confirmed_at, time=ev.time))
        prev_dir = ev.direction
    _advance_states(zones, klines)
    return zones


# ------------------------------------------------------------------ #
# 狀態推進（觸及 / 翻轉 / 走完）
# ------------------------------------------------------------------ #
def _advance_states(zones: list[Zone], klines) -> None:
    for z in zones:
        for i in range(z.created_at + 1, len(klines)):
            k = klines[i]
            lo, hi, cl = ohlc.l(k), ohlc.h(k), ohlc.c(k)
            touched = lo <= z.top and hi >= z.bottom
            if z.direction == Direction.UP:
                flipped = cl < z.bottom          # 收盤穿越遠端
                swept_through = lo <= z.bottom   # 影線走完整區
            else:
                flipped = cl > z.top
                swept_through = hi >= z.top

            if flipped:
                z.state = ZoneState.FLIPPED
                z.state_changed_at = i
                z.direction = (Direction.DOWN if z.direction == Direction.UP
                               else Direction.UP)          # 極性反轉
                if z.kind == ZoneKind.OB or z.kind == ZoneKind.PB:
                    z.kind = ZoneKind.BREAKER
                elif z.kind == ZoneKind.FVG:
                    z.kind = ZoneKind.IFVG
                break
            if swept_through and z.state == ZoneState.TESTED:
                z.state = ZoneState.FILLED
                z.state_changed_at = i
                break
            if touched and z.state == ZoneState.FRESH:
                z.state = ZoneState.TESTED
                z.state_changed_at = i


# ------------------------------------------------------------------ #
# BPR / Premium-Discount / OTE
# ------------------------------------------------------------------ #
def detect_bprs(fvgs: list[Zone],
                max_bars_apart: Optional[int] = 50) -> list[Zone]:
    """多頭 FVG 與空頭 FVG 的區域交集；方向依後形成者。

    max_bars_apart：兩個 FVG 形成時間相距超過此根數就不配對。
    講義的 BPR 是「同一場多空交戰中緊接形成」的兩個缺口；
    不設限會讓相隔數百根的缺口互相配對，組合爆炸成上千個無意義區域。
    設 None 可關閉限制（回到純數學交集）。"""
    out: list[Zone] = []
    bulls = [z for z in fvgs if z.direction == Direction.UP
             or (z.kind == ZoneKind.IFVG and z.direction == Direction.DOWN)]
    bears = [z for z in fvgs if z.direction == Direction.DOWN
             or (z.kind == ZoneKind.IFVG and z.direction == Direction.UP)]
    for a in bulls:
        for b in bears:
            if max_bars_apart is not None \
                    and abs(a.created_at - b.created_at) > max_bars_apart:
                continue
            top = min(a.top, b.top)
            bottom = max(a.bottom, b.bottom)
            if top <= bottom:
                continue
            later = a if a.created_at >= b.created_at else b
            out.append(Zone(ZoneKind.BPR, top, bottom, later.direction,
                            created_at=later.created_at, time=later.time))
    return out


def fib_price(range_high: float, range_low: float, f: float) -> float:
    """OTE 錨定：0 定在 range_high、1 定在 range_low。"""
    return range_high - f * (range_high - range_low)


def discount_ceiling(range_high: float, range_low: float) -> float:
    """Discount 半區上限（0.5）：做多只在此價位以下找進場區。"""
    return fib_price(range_high, range_low, 0.5)


def ote_band(range_high: float, range_low: float,
             ote: tuple[float, float] = (0.62, 0.79)) -> tuple[float, float]:
    """OTE 價帶（回傳 low, high）。fib 0.62–0.79 之間。"""
    a = fib_price(range_high, range_low, ote[0])
    b = fib_price(range_high, range_low, ote[1])
    return (min(a, b), max(a, b))
