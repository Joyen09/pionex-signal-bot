"""流動性：池（swing / EQH-EQL / 前日高低 / 時段高低）與掃蕩（sweep）。

- swing high 上方 = BUY_SIDE（空單止損聚集）；swing low 下方 = SELL_SIDE。
- EQH/EQL：多個 swing 價差在容忍度內合併為一池，價位取成員極值。
- Sweep（wick 模式）：單根 K 影線刺穿池但收盤收回 → 成立，記錄影線極值。
- 池被收盤有效突破且未在 max_reclaim_bars 內收回 → consumed，不再作為目標。
"""
from __future__ import annotations

from typing import Optional

from . import ohlc
from .types import (LiquidityPool, PoolSide, PoolSource, SweepEvent, SwingKind,
                    SwingPoint)


def build_pools(swings: list[SwingPoint],
                eq_tolerance_pct: float = 0.001) -> list[LiquidityPool]:
    """把已確認 swing 聚成流動性池；相近價位（<= 容忍度）合併為 EQ 池。"""
    pools: list[LiquidityPool] = []
    for kind, side in ((SwingKind.HIGH, PoolSide.BUY_SIDE),
                       (SwingKind.LOW, PoolSide.SELL_SIDE)):
        group = sorted([s for s in swings if s.kind == kind], key=lambda s: s.price)
        cluster: list[SwingPoint] = []
        for s in group:
            if cluster and abs(s.price - cluster[-1].price) / cluster[-1].price \
                    > eq_tolerance_pct:
                pools.append(_make_pool(cluster, side))
                cluster = []
            cluster.append(s)
        if cluster:
            pools.append(_make_pool(cluster, side))
    return pools


def _make_pool(members: list[SwingPoint], side: PoolSide) -> LiquidityPool:
    prices = [m.price for m in members]
    # EQH 取最高、EQL 取最低（池價位 = 成員極值）
    price = max(prices) if side == PoolSide.BUY_SIDE else min(prices)
    return LiquidityPool(
        price=price, side=side,
        source=PoolSource.EQ if len(members) >= 2 else PoolSource.SWING,
        members=list(members),
        created_at=max(m.confirmed_at for m in members),
    )


def daily_pools(klines) -> list[LiquidityPool]:
    """前日高低（PDH/PDL）。需要 K 線帶毫秒時間戳；跨日以 UTC 日界計。"""
    pools: list[LiquidityPool] = []
    cur_day = None
    day_hi = day_lo = None
    day_start_idx = 0
    for i, k in enumerate(klines):
        ts = ohlc.t(k)
        if ts is None:
            return []            # 沒時間戳就無法分日
        day = int(float(ts) // 86_400_000)   # 毫秒 → UTC 日
        if cur_day is None:
            cur_day, day_hi, day_lo, day_start_idx = day, ohlc.h(k), ohlc.l(k), i
            continue
        if day != cur_day:
            # 昨日結束 → 昨日高低成為今天的池（created_at = 今日第一根）
            hi_sp = SwingPoint(day_start_idx, None, day_hi, SwingKind.HIGH, i)
            lo_sp = SwingPoint(day_start_idx, None, day_lo, SwingKind.LOW, i)
            pools.append(LiquidityPool(day_hi, PoolSide.BUY_SIDE,
                                       PoolSource.PDH_PDL, [hi_sp], created_at=i))
            pools.append(LiquidityPool(day_lo, PoolSide.SELL_SIDE,
                                       PoolSource.PDH_PDL, [lo_sp], created_at=i))
            cur_day, day_hi, day_lo, day_start_idx = day, ohlc.h(k), ohlc.l(k), i
        else:
            day_hi = max(day_hi, ohlc.h(k))
            day_lo = min(day_lo, ohlc.l(k))
    return pools


def detect_sweeps(klines, pools: list[LiquidityPool],
                  mode: str = "wick",
                  max_reclaim_bars: int = 3) -> list[SweepEvent]:
    """對每個池找掃蕩事件，並標記被有效突破（consumed）的池。

    wick 模式（SELL_SIDE）：low < pool.price 且 close > pool.price → Sweep。
    reclaim 模式：收盤跌破後 max_reclaim_bars 內收盤收回 → Sweep（記在收回根）。
    收盤突破且未在窗內收回 → pool.consumed = True。
    """
    events: list[SweepEvent] = []
    for pool in pools:
        breach_at: Optional[int] = None   # reclaim 模式：收盤突破的位置
        for i in range(pool.created_at, len(klines)):
            if pool.consumed:
                break
            k = klines[i]
            lo, hi, cl = ohlc.l(k), ohlc.h(k), ohlc.c(k)
            if pool.side == PoolSide.SELL_SIDE:
                pierced = lo < pool.price
                closed_beyond = cl < pool.price
                reclaimed = cl > pool.price
                extreme = lo
            else:
                pierced = hi > pool.price
                closed_beyond = cl > pool.price
                reclaimed = cl < pool.price
                extreme = hi

            if mode == "wick":
                if pierced and not closed_beyond:
                    events.append(SweepEvent(pool, extreme, confirmed_at=i,
                                             time=ohlc.t(k)))
                    break
                if closed_beyond:
                    # 收盤突破：看 max_reclaim_bars 內是否收回，否則 consumed
                    if _reclaims_within(klines, pool, i, max_reclaim_bars) is None:
                        pool.consumed = True
                        pool.consumed_at = i
                    break
            else:  # reclaim 模式
                if breach_at is None:
                    if closed_beyond:
                        breach_at = i
                else:
                    if reclaimed:
                        ext = _extreme_between(klines, pool, breach_at, i)
                        events.append(SweepEvent(pool, ext, confirmed_at=i,
                                                 time=ohlc.t(k)))
                        break
                    if i - breach_at >= max_reclaim_bars:
                        pool.consumed = True
                        pool.consumed_at = i
                        break
    return events


def _reclaims_within(klines, pool: LiquidityPool, breach_idx: int,
                     window: int) -> Optional[int]:
    for j in range(breach_idx + 1, min(breach_idx + 1 + window, len(klines))):
        cl = ohlc.c(klines[j])
        if (pool.side == PoolSide.SELL_SIDE and cl > pool.price) or \
           (pool.side == PoolSide.BUY_SIDE and cl < pool.price):
            return j
    return None


def _extreme_between(klines, pool: LiquidityPool, a: int, b: int) -> float:
    seg = klines[a:b + 1]
    if pool.side == PoolSide.SELL_SIDE:
        return min(ohlc.l(k) for k in seg)
    return max(ohlc.h(k) for k in seg)
