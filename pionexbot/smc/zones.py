"""進場區域：OB / PB / Breaker、FVG / IFVG / BPR、Premium-Discount / OTE。

狀態機（v1.1 生命週期，隨後續 K 線推進，時間記在轉變當根）：
    FRESH →（價格觸及）TESTED →（收盤穿越遠端）FLIPPED（OB→Breaker、FVG→IFVG，僅此一次）
    翻轉後再次收盤穿越 → 移除（removed_at）
    BPR 收盤穿越遠端 → 直接移除（不翻轉）
    影線走完整個區域但未收盤穿越 → FILLED（同樣視為移除）
    全域：存活超過 max_age_bars → 移除；同 kind 活躍數超過上限 → 移除最舊

單一真相來源：plot 與策略一律吃 detect_all() 的輸出，禁止各自另做過濾。
"""
from __future__ import annotations

from typing import Optional

from . import ohlc
from .types import (Direction, StructureEvent, Zone, ZoneKind, ZoneState)


# ------------------------------------------------------------------ #
# FVG
# ------------------------------------------------------------------ #
def detect_fvgs(klines, min_size_pct: float = 0.0005,
                max_age_bars: Optional[int] = 200) -> list[Zone]:
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
    _advance_states(zones, klines, max_age_bars=max_age_bars)
    return zones


# ------------------------------------------------------------------ #
# OB / PB
# ------------------------------------------------------------------ #
def detect_obs(klines, events: list[StructureEvent],
               zone_mode: str = "full_range",
               max_age_bars: Optional[int] = 200) -> list[Zone]:
    """每次 BOS/MSS 確認時回溯：推進段中最後一根「反向實體」K 即訂單塊。

    向上突破 → 找突破根之前最後一根收黑（close < open）的 K。
    PB：趨勢延續（BOS 且前一事件同向）中生成的同向 OB。
    v1.1：只有 displacement=true 的事件才生成 OB/PB（弱勢突破的回溯 K
    不代表機構參與，盤整期會產出大量雜訊塊）。
    """
    zones: list[Zone] = []
    prev_dir: Optional[Direction] = None
    for ev in events:
        if not getattr(ev, "displacement", True):
            prev_dir = ev.direction
            continue
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
    _advance_states(zones, klines, max_age_bars=max_age_bars)
    return zones


# ------------------------------------------------------------------ #
# 狀態推進（觸及 / 翻轉一次 / 再穿移除 / 走完 / TTL）
# ------------------------------------------------------------------ #
def _advance_states(zones: list[Zone], klines,
                    max_age_bars: Optional[int] = 200) -> None:
    for z in zones:
        flips = 0
        for i in range(z.created_at + 1, len(klines)):
            if max_age_bars is not None and i - z.created_at > max_age_bars:
                z.removed_at = i                 # TTL：逾齡移除
                break
            k = klines[i]
            lo, hi, cl = ohlc.l(k), ohlc.h(k), ohlc.c(k)
            touched = lo <= z.top and hi >= z.bottom
            if z.direction == Direction.UP:
                closed_through = cl < z.bottom   # 收盤穿越遠端
                swept_through = lo <= z.bottom   # 影線走完整區
            else:
                closed_through = cl > z.top
                swept_through = hi >= z.top

            if closed_through:
                if z.kind == ZoneKind.BPR or flips >= 1:
                    # BPR 不翻轉；其他區域只翻一次，再次收盤穿越 → 移除
                    z.state_changed_at = i
                    z.removed_at = i
                    break
                flips = 1
                z.state = ZoneState.FLIPPED
                z.state_changed_at = i
                z.direction = (Direction.DOWN if z.direction == Direction.UP
                               else Direction.UP)          # 極性反轉
                if z.kind == ZoneKind.OB or z.kind == ZoneKind.PB:
                    z.kind = ZoneKind.BREAKER
                elif z.kind == ZoneKind.FVG:
                    z.kind = ZoneKind.IFVG
                continue                         # 翻轉後繼續追蹤（等再穿或 TTL）
            if swept_through and z.state == ZoneState.TESTED:
                z.state = ZoneState.FILLED
                z.state_changed_at = i
                z.removed_at = i                 # 走完 = 不再有交易意義
                break
            if touched and z.state == ZoneState.FRESH:
                z.state = ZoneState.TESTED
                z.state_changed_at = i


# ------------------------------------------------------------------ #
# BPR / Premium-Discount / OTE
# ------------------------------------------------------------------ #
def _overlap_ratio(a: Zone, b: Zone) -> float:
    """兩區域的價格重疊量 / 較窄者高度。"""
    inter = min(a.top, b.top) - max(a.bottom, b.bottom)
    shorter = min(a.top - a.bottom, b.top - b.bottom)
    return inter / shorter if shorter > 0 else 0.0


def detect_bprs(fvgs: list[Zone], klines=None, *,
                max_gap_bars: Optional[int] = 20,
                require_overlap_leg: bool = True,
                dedup_overlap_pct: float = 0.5,
                min_size_pct: float = 0.0005,
                component_min_pct: float = 0.0015,
                max_age_bars: Optional[int] = 200) -> list[Zone]:
    """BPR（v1.1）：「同一價區先後被兩個方向的位移充分傳遞」，全部滿足才成立：

    1. 時間先後＋相鄰：兩個反向 FVG 間隔 ≤ max_gap_bars（None 可關閉，僅供測試）
    2. 位移穿越：require_overlap_leg 時，第二個 FVG 的位移 K（c2）範圍
       必須實際走完第一個 FVG 的整個區域（需要 klines；未給則略過此檢查）
    3. 兩者當時皆未失效：配對瞬間第一個 FVG 不得已被移除（FILLED/再穿/TTL）
    4. 自身寬度門檻：交集寬度/現價 ≥ min_size_pct（與 FVG 同標準，交集不豁免）
    5. 去重：與既有 BPR 重疊比例 > dedup_overlap_pct → 只留較新者
    6. 組件位移級：兩個 FVG 本身寬度/現價 ≥ component_min_pct——BPR 的定義
       是「被兩個方向的位移**充分**傳遞」，與位移判定同一把尺
       （displacement_fvg_min_pct）。密度實測：0.05% 的雜訊缺口互相交集
       產出的細條佔 BPR 族群 2/3，gap/dedup 都推不動，這才是正確的旋鈕。

    方向依後形成者的「原始極性」（配對當下它還沒被翻轉）。
    v1.0 的純數學交集在盤整段會組合爆炸（實測 10 天 BTC 15M 生出 300+ 個
    $45 寬的雜訊細條、佔全部區域 73%）——這就是本函數改版的原因。"""
    pairs: list[Zone] = []
    bulls = [z for z in fvgs if z.direction == Direction.UP
             or (z.kind == ZoneKind.IFVG and z.direction == Direction.DOWN)]
    bears = [z for z in fvgs if z.direction == Direction.DOWN
             or (z.kind == ZoneKind.IFVG and z.direction == Direction.UP)]
    for a in bulls:
        for b in bears:
            first, second = (a, b) if a.created_at <= b.created_at else (b, a)
            gap = second.created_at - first.created_at
            if max_gap_bars is not None and gap > max_gap_bars:
                continue                                   # 規則 1
            top = min(a.top, b.top)
            bottom = max(a.bottom, b.bottom)
            if top <= bottom:
                continue
            if first.removed_at is not None \
                    and first.removed_at <= second.created_at:
                continue                                   # 規則 3
            if require_overlap_leg and klines is not None:
                c2i = second.created_at - 1                # FVG 的位移 K（中間根）
                if c2i < 0 or not (ohlc.l(klines[c2i]) <= first.bottom
                                   and ohlc.h(klines[c2i]) >= first.top):
                    continue                               # 規則 2
            px = ohlc.c(klines[second.created_at]) if klines is not None \
                else (top + bottom) / 2
            if px <= 0 or (top - bottom) / px < min_size_pct:
                continue                                   # 規則 4
            if (a.top - a.bottom) / px < component_min_pct \
                    or (b.top - b.bottom) / px < component_min_pct:
                continue                                   # 規則 6
            direction = Direction.UP if second is a else Direction.DOWN
            pairs.append(Zone(ZoneKind.BPR, top, bottom, direction,
                              created_at=second.created_at, time=second.time))
    # 規則 5：去重（時間序處理，重疊過高只留較新者）
    pairs.sort(key=lambda z: z.created_at)
    kept: list[Zone] = []
    for z in pairs:
        kept = [w for w in kept if _overlap_ratio(w, z) <= dedup_overlap_pct]
        kept.append(z)
    if klines is not None:
        _advance_states(kept, klines, max_age_bars=max_age_bars)
    return kept


def _cap_active_per_kind(zones: list[Zone], max_active: int) -> None:
    """同一 kind 的活躍區域超過上限 → 移除最舊者（依時間序模擬）。

    kind 以最終狀態分組（翻轉會改 kind，逐根重分組成本不值得——上限的
    目的是防洪，不是精確配額）。"""
    groups: dict = {}
    for z in zones:
        groups.setdefault(z.kind, []).append(z)
    for arr in groups.values():
        arr.sort(key=lambda z: z.created_at)
        for z in arr:
            active = [w for w in arr
                      if w.created_at < z.created_at
                      and (w.removed_at is None
                           or w.removed_at > z.created_at)]
            overflow = len(active) + 1 - max_active
            if overflow > 0:
                for w in sorted(active, key=lambda w: w.created_at)[:overflow]:
                    w.removed_at = z.created_at


def detect_all(klines, smc_cfg: Optional[dict] = None,
               events: Optional[list[StructureEvent]] = None) -> list[Zone]:
    """單一真相來源（v1.1）：FVG/IFVG + OB/PB/Breaker + BPR 一次算完，
    含完整生命週期（翻轉一次、再穿移除、走完、TTL、同類上限）。

    plot 與策略一律吃這個輸出；禁止在下游另做過濾（除了策略本來的
    「方向/半區/新舊」選區條件）。events 不給時自行跑 detect_structure。"""
    sc = smc_cfg or {}
    zcfg = sc.get("zones", {})
    raw_age = zcfg.get("max_age_bars", 200)
    max_age = int(raw_age) if raw_age is not None else None
    max_active = int(zcfg.get("max_active_per_kind", 8))
    fvg_min = float(sc.get("fvg", {}).get("min_size_pct", 0.0005))
    if events is None:
        from . import structure as _structure
        swing = sc.get("swing", {})
        events = _structure.detect_structure(
            klines, left=int(swing.get("left", 3)),
            right=int(swing.get("right", 3)), smc_cfg=sc).events
    fvgs = detect_fvgs(klines, fvg_min, max_age_bars=max_age)
    obs = detect_obs(klines, events,
                     zone_mode=str(sc.get("ob", {}).get("zone", "full_range")),
                     max_age_bars=max_age)
    bcfg = sc.get("bpr", {})
    raw_gap = bcfg.get("max_gap_bars", bcfg.get("max_bars_apart", 20))
    # 組件門檻預設跟位移判定同一把尺（可用 bpr.component_min_pct 覆寫）
    comp_min = float(bcfg.get(
        "component_min_pct",
        sc.get("structure", {}).get("displacement_fvg_min_pct", 0.0015)))
    bprs = detect_bprs(
        fvgs, klines,
        max_gap_bars=int(raw_gap) if raw_gap is not None else None,
        require_overlap_leg=bool(bcfg.get("require_overlap_leg", True)),
        dedup_overlap_pct=float(bcfg.get("dedup_overlap_pct", 0.5)),
        min_size_pct=fvg_min, component_min_pct=comp_min,
        max_age_bars=max_age)
    out = fvgs + obs + bprs
    if max_active:
        _cap_active_per_kind(out, max_active)
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
