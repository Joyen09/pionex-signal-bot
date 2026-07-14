"""ict2022 做多策略：組裝 SMC 偵測模組（規格 §4）。

八步流水線（每根 entry 時框收盤評估）：
  1. HTF 偏向：高階時框結構為 UP 才做多
  2. Liquidity Grab：trigger 時框近期掃過 SELL_SIDE 池（sweep）
  3. MSS：sweep 之後 trigger 出現看漲 MSS
  4. 進場區：MSS 推進段的 Discount 半區挑區域（BPR > IFVG > FVG > PB > OB），
     掛限價於 CE（或區域上緣）
  5. SL = min(sweep 收針極值, 區域下緣) × (1 − sl_buffer_pct)
  6. TP：上方 BUY_SIDE 池由近到遠 1–3 個；TP1 不足 min_rr_tp1 就放棄
  7. 失效：expiry_bars 未成交 / 未回踩即創新高 / 反向 MSS → 撤單（由呼叫端執行）
  8. killzone / 每日上限 / 消息封鎖過濾（每日上限與消息封鎖走既有風控）

此模組只做「計畫」，是純函數；下單與撤單由 runner / 回測器執行，
兩者共用同一套本檔邏輯（規格 §1：回測與實盤共用偵測程式碼）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import TakeProfit
from ..smc import liquidity, ohlc, structure, zones
from ..smc.types import (Direction, PoolSide, StructureKind, SwingKind,
                         ZoneKind, ZoneState)

# 區域優先權（講義規則）：值越小越優先
_ZONE_PRIORITY = {ZoneKind.BPR: 0, ZoneKind.IFVG: 1, ZoneKind.FVG: 2,
                  ZoneKind.PB: 3, ZoneKind.OB: 4}

DEFAULTS = {
    "htf_interval": "4H",
    "trigger_interval": "15M",
    "entry_interval": "5M",
    "lookback_sweep_bars": 60,
    "entry_price_mode": "ce",        # ce | zone_edge
    "sl_buffer_pct": 0.001,
    "tp_allocation": [0.5, 0.3, 0.2],
    "min_rr_tp1": 1.5,
    "expiry_bars": 24,
    "killzone_filter": True,
    "require_ote": False,     # True = 進場區 CE 必須落在 OTE 帶（硬條件）
    "sweep_sources": [],      # 限定觸發 sweep 的池種（如 ["EQ"]）；空 = 不限
}


@dataclass
class Setup:
    """一個待掛的做多 setup（限價單計畫）。"""

    limit_price: float
    stop_loss: float
    take_profits: list[TakeProfit]
    range_high: float                # 未回踩即創新高 → 失效
    sweep_at: int                    # trigger 時框上 sweep 確認索引（識別 setup 用）
    zone_created_at: int
    expiry_bars: int
    tags: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple:
        """同一個 setup 只掛一次。用價位當識別——sweep_at/zone_created_at 是
        「評估視窗內」的相對索引，視窗滑動後會變，價位不會。"""
        return (round(self.limit_price, 8), round(self.stop_loss, 8),
                round(self.range_high, 8))


def find_setup(htf_klines, trigger_klines, cfg: dict,
               smc_cfg: Optional[dict] = None,
               killzone: Optional[str] = None,
               funnel: Optional[dict] = None) -> Optional[Setup]:
    """依八步流水線評估；不成立回 None。輸入一律為「已收盤」K 線。

    funnel：選填的計數器 dict——每一關擋掉時 +1，用來診斷
    「為什麼都沒 setup」（哪一關太嚴一目了然）。"""
    def _block(reason: str) -> None:
        if funnel is not None:
            funnel[reason] = funnel.get(reason, 0) + 1

    p = {**DEFAULTS, **(cfg or {})}
    sc = smc_cfg or {}
    swing_cfg = sc.get("swing", {})
    left = int(swing_cfg.get("left", 3))
    right = int(swing_cfg.get("right", 3))

    if len(trigger_klines) < 30 or len(htf_klines) < 20:
        _block("資料不足")
        return None

    # 8) killzone 過濾（entry 當下時段由呼叫端判好傳入）
    if p["killzone_filter"] and killzone is None:
        _block("killzone 外")
        return None

    # 1) HTF 偏向：結構 UP 才做多（狀態機不分位移強弱——偏向要即時翻面）
    st_h = structure.detect_structure(htf_klines, left=left, right=right,
                                      smc_cfg=sc)
    if not st_h.trend or st_h.trend[-1] != Direction.UP:
        _block("HTF 非多頭")
        return None

    # 2) 近期 sweep 了 SELL_SIDE 池
    swings = structure.find_swings(trigger_klines, left, right)
    liq = sc.get("liquidity", {})
    pools = liquidity.build_pools(
        swings, float(liq.get("eq_tolerance_pct", 0.001)))
    pools += liquidity.daily_pools(trigger_klines)
    sweeps = liquidity.detect_sweeps(
        trigger_klines, pools,
        mode=str(liq.get("sweep_mode", "wick")),
        max_reclaim_bars=int(liq.get("max_reclaim_bars", 3)))
    horizon = len(trigger_klines) - int(p["lookback_sweep_bars"])
    recent = [s for s in sweeps
              if s.pool.side == PoolSide.SELL_SIDE and s.confirmed_at >= horizon]
    if not recent:
        _block("近期無 SELL_SIDE sweep")
        return None
    allowed = {str(x).upper() for x in (p.get("sweep_sources") or [])}
    if allowed:
        recent = [s for s in recent if s.pool.source.value in allowed]
        if not recent:
            _block("sweep 池種不符")
            return None
    sweep = max(recent, key=lambda s: s.confirmed_at)

    # 3) sweep 之後的看漲 MSS（v1.1：只認 displacement=true 的事件——
    #    弱勢突破在盤整裡大量出現，不是機構換手的證據）
    st_t = structure.detect_structure(trigger_klines, swings, smc_cfg=sc)
    mss_list = [e for e in st_t.events
                if e.kind == StructureKind.MSS and e.direction == Direction.UP
                and e.confirmed_at >= sweep.confirmed_at and e.displacement]
    if not mss_list:
        _block("sweep 後無看漲 MSS")
        return None
    mss = mss_list[-1]

    # 7) 反向 MSS 出現在其後 → 本 setup 已失效
    if any(e.kind == StructureKind.MSS and e.direction == Direction.DOWN
           and e.confirmed_at > mss.confirmed_at for e in st_t.events):
        _block("已出現反向 MSS")
        return None

    # 4) dealing range 與 Discount 半區
    range_low = sweep.wick_extreme
    after = [s for s in swings if s.kind == SwingKind.HIGH
             and s.index >= mss.confirmed_at
             and s.confirmed_at <= len(trigger_klines) - 1]
    if after:
        range_high = max(s.price for s in after)
    else:  # 推進段還沒做出已確認 swing high → 用段內最高價保守代替
        range_high = max(ohlc.h(k) for k in trigger_klines[mss.confirmed_at:])
    if range_high <= range_low:
        _block("range 無效")
        return None
    ceiling = zones.discount_ceiling(range_high, range_low)
    ote_lo, ote_hi = zones.ote_band(range_high, range_low,
                                    tuple(sc.get("fib", {}).get("ote", (0.62, 0.79))))

    # 進場區域：sweep 之後形成、看漲、活躍（未移除/未過 TTL）、CE 在 Discount
    # 半區。detect_all = 單一真相來源（生命週期、BPR 五規則、同類上限都在裡面）
    now = len(trigger_klines) - 1
    cands = [z for z in zones.detect_all(trigger_klines, sc, st_t.events)
             if z.direction == Direction.UP
             and z.state in (ZoneState.FRESH, ZoneState.TESTED)
             and z.is_active(now)
             and z.created_at >= sweep.confirmed_at
             and z.ce <= ceiling]
    if not cands:
        _block("Discount 無可用區域")
        return None
    def in_ote(z) -> bool:
        return ote_lo <= z.ce <= ote_hi
    if p["require_ote"]:
        cands = [z for z in cands if in_ote(z)]
        if not cands:
            _block("OTE 外")
            return None
    cands.sort(key=lambda z: (_ZONE_PRIORITY.get(z.kind, 9),
                              0 if in_ote(z) else 1, -z.created_at))
    zone = cands[0]
    entry = zone.ce if p["entry_price_mode"] == "ce" else zone.top

    # 5) SL
    sl = min(sweep.wick_extreme, zone.bottom) * (1 - float(p["sl_buffer_pct"]))
    if sl >= entry:
        _block("SL 高於進場價")
        return None

    # 6) TP：entry 上方最近的 BUY_SIDE 池 1–3 個 + RR 檢查
    targets = sorted({pl.price for pl in pools
                      if pl.side == PoolSide.BUY_SIDE and not pl.consumed
                      and pl.price > entry})
    alloc = list(p["tp_allocation"])
    targets = targets[:len(alloc)]
    if not targets:
        _block("上方無 TP 池")
        return None
    rr = (targets[0] - entry) / (entry - sl)
    if rr < float(p["min_rr_tp1"]):
        _block("RR 不足")
        return None
    # 比例重新正規化到實際 TP 數量（少於 3 個時最後一段吃剩餘）
    alloc = alloc[:len(targets)]
    alloc[-1] = round(1.0 - sum(alloc[:-1]), 10)
    tps = [TakeProfit(price=t, fraction=f) for t, f in zip(targets, alloc)]

    _block("setup 成立")
    return Setup(
        limit_price=entry, stop_loss=sl, take_profits=tps,
        range_high=range_high, sweep_at=sweep.confirmed_at,
        zone_created_at=zone.created_at,
        expiry_bars=int(p["expiry_bars"]),
        tags={"swept_pool": sweep.pool.source.value,
              "zone_kind": zone.kind.value,
              "in_ote": in_ote(zone),
              "killzone": killzone or "",
              "rr_tp1": round(rr, 2),
              # v1.1 診斷：setup 成立當下的候選區域數。
              # 修正後應為個位數；幾十個代表選區形同隨機（退化未修好）
              "candidates": len(cands)},
    )
