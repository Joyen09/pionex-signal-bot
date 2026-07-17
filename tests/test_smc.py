"""Phase 2 SMC 偵測模組測試：合成 K 線正反例 + 防重繪（no repaint）。

執行：python tests/test_smc.py  或  python -m pytest tests/ -v
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.smc import liquidity, sessions, structure, zones  # noqa: E402
from pionexbot.smc.types import (Direction, PoolSide, PoolSource,  # noqa: E402
                                 StructureKind, SwingKind, ZoneKind, ZoneState)


def bar(o, h, l, c, t=None):  # noqa: E741
    return {"open": o, "high": h, "low": l, "close": c, "time": t}


# ------------------------------------------------------------------ #
# Swing point（分形）
# ------------------------------------------------------------------ #
def test_swing_high_detected_and_confirmed_at_right():
    ks = [bar(9.5, 10, 9, 9.5), bar(10, 12, 10, 11), bar(10.5, 11, 9.5, 10)]
    sw = structure.find_swings(ks, left=1, right=1)
    highs = [s for s in sw if s.kind == SwingKind.HIGH]
    assert len(highs) == 1
    assert highs[0].index == 1 and highs[0].price == 12
    assert highs[0].confirmed_at == 2, "確認時間 = index + right"


def test_equal_highs_are_not_swing():
    ks = [bar(9, 10, 9, 9.5), bar(10, 12, 10, 11), bar(10, 12, 10, 11),
          bar(10, 11, 9.5, 10)]
    sw = structure.find_swings(ks, left=1, right=1)
    assert not [s for s in sw if s.kind == SwingKind.HIGH], \
        "等高（嚴格不等式不成立）不得判為 swing，交給 EQH 處理"


# ------------------------------------------------------------------ #
# BOS / MSS 狀態機
# ------------------------------------------------------------------ #
def _structure_fixture():
    return [
        bar(9.8, 10.0, 9.0, 9.5),
        bar(10.0, 12.0, 10.0, 11.0),   # swing high 12 @1（conf 2）
        bar(11.0, 11.0, 9.5, 10.0),
        bar(10.0, 10.5, 9.0, 9.5),     # swing low 9 @3（conf 4）
        bar(9.6, 11.0, 9.6, 10.8),
        bar(11.0, 12.6, 11.0, 12.61),  # 收盤破 12 → BOS UP @5
        bar(12.6, 13.0, 11.0, 12.8),   # swing high 13 @6（conf 7）
        bar(12.5, 12.5, 10.0, 11.0),
        bar(10.5, 10.5, 8.5, 8.8),     # 收盤破 protective low 9 → MSS DOWN @8
    ]


def test_bos_then_mss():
    st = structure.detect_structure(_structure_fixture(), left=1, right=1)
    kinds = [(e.kind, e.direction, e.confirmed_at) for e in st.events]
    assert (StructureKind.BOS, Direction.UP, 5) in kinds, f"事件：{kinds}"
    assert (StructureKind.MSS, Direction.DOWN, 8) in kinds, f"事件：{kinds}"
    bos = next(e for e in st.events if e.kind == StructureKind.BOS)
    assert bos.broken_level == 12.0
    mss = next(e for e in st.events if e.kind == StructureKind.MSS)
    assert mss.broken_level == 9.0, "MSS 觸發線 = 造成 BOS 的 higher-low"
    assert st.trend[5] == Direction.UP and st.trend[8] == Direction.DOWN


def test_no_events_without_breaks():
    ks = [bar(10, 10.5, 9.5, 10)] * 10   # 完全橫盤 → 無 swing 突破
    st = structure.detect_structure(ks, left=1, right=1)
    assert st.events == []


# ------------------------------------------------------------------ #
# 流動性：EQ 合併、Sweep、consumed
# ------------------------------------------------------------------ #
def test_eqh_pools_merge_within_tolerance():
    from pionexbot.smc.types import SwingPoint
    sw = [SwingPoint(1, None, 100.00, SwingKind.HIGH, 2),
          SwingPoint(5, None, 100.05, SwingKind.HIGH, 6),   # 差 0.05% < 0.1%
          SwingPoint(9, None, 103.00, SwingKind.HIGH, 10)]  # 獨立池
    pools = liquidity.build_pools(sw, eq_tolerance_pct=0.001)
    assert len(pools) == 2
    eq = next(p for p in pools if p.source == PoolSource.EQ)
    assert eq.price == 100.05 and len(eq.members) == 2, "EQH 取成員最高價"
    assert eq.created_at == 6, "池成形 = 最後一個成員確認"


def test_sweep_wick_below_sell_side_pool():
    ks = [
        bar(9.6, 10.0, 9.5, 9.8),
        bar(9.5, 9.8, 9.0, 9.4),      # swing low 9.0 @1（conf 2）
        bar(9.5, 10.0, 9.6, 9.9),
        bar(9.8, 9.9, 8.8, 9.3),      # 影線刺 8.8 < 9.0、收 9.3 > 9.0 → Sweep
    ]
    sw = structure.find_swings(ks, left=1, right=1)
    pools = liquidity.build_pools(sw)
    ev = liquidity.detect_sweeps(ks, pools)
    assert len(ev) == 1
    assert ev[0].confirmed_at == 3 and ev[0].wick_extreme == 8.8
    assert ev[0].pool.side == PoolSide.SELL_SIDE


def test_close_through_pool_marks_consumed_no_sweep():
    ks = [
        bar(9.6, 10.0, 9.5, 9.8),
        bar(9.5, 9.8, 9.0, 9.4),      # swing low 9.0 @1
        bar(9.5, 10.0, 9.6, 9.9),
        bar(9.6, 9.7, 8.5, 8.7),      # 收盤跌破池
        bar(8.7, 8.9, 8.4, 8.6),      # 三根內都沒收回
        bar(8.6, 8.8, 8.3, 8.5),
        bar(8.5, 8.7, 8.2, 8.4),
    ]
    sw = structure.find_swings(ks, left=1, right=1)
    pools = liquidity.build_pools(sw)
    ev = liquidity.detect_sweeps(ks, pools, max_reclaim_bars=3)
    assert ev == [], "有效突破不是 sweep"
    sell = next(p for p in pools if p.side == PoolSide.SELL_SIDE)
    assert sell.consumed, "被收盤突破且未收回 → consumed"


# ------------------------------------------------------------------ #
# FVG / IFVG / OB / BPR / OTE
# ------------------------------------------------------------------ #
def test_bullish_fvg_bounds_and_state_transitions():
    ks = [
        bar(9.5, 10.0, 9.0, 9.8),                 # c1：high 10
        bar(10.0, 10.8, 9.9, 10.7),               # c2 大陽
        bar(10.6, 11.2, 10.5, 11.0),              # c3：low 10.5 > 10 → FVG [10,10.5]
        bar(11.0, 11.1, 10.4, 10.9),              # 觸及區域 → TESTED
        bar(10.8, 10.9, 9.7, 9.8),                # 收盤 < 10 → 翻轉 IFVG
    ]
    zs = zones.detect_fvgs(ks, min_size_pct=0.0005)
    assert len(zs) == 1
    z = zs[0]
    assert (z.bottom, z.top) == (10.0, 10.5) and z.created_at == 2
    assert z.kind == ZoneKind.IFVG and z.state == ZoneState.FLIPPED
    assert z.direction == Direction.DOWN, "翻轉後極性反轉"
    assert abs(z.ce - 10.25) < 1e-9


def test_tiny_fvg_filtered_by_min_size():
    ks = [bar(100, 100.0, 99.5, 99.9), bar(100, 100.3, 99.9, 100.2),
          bar(100.2, 100.4, 100.01, 100.3)]   # 缺口 0.01 → 0.01% < 0.05%
    assert zones.detect_fvgs(ks, min_size_pct=0.0005) == []


def test_ob_is_last_bearish_candle_before_up_break():
    ks = [
        bar(10.0, 10.2, 9.6, 9.8),
        bar(10.0, 10.5, 9.8, 10.4),    # swing high 10.5 @1（conf 2）
        bar(10.2, 10.3, 9.5, 9.7),     # 最後一根收黑 → OB 候選
        bar(9.8, 10.9, 9.8, 10.8),     # 收盤破 10.5 → BOS UP @3
    ]
    # 本測試只驗 OB 回溯邏輯；4 根迷你劇本湊不出推進段 FVG，
    # 位移判定用 atr 模式（大實體突破 → 強事件）
    st = structure.detect_structure(
        ks, left=1, right=1,
        smc_cfg={"structure": {"displacement_mode": "atr"}})
    obs = zones.detect_obs(ks, st.events)
    assert len(obs) == 1
    z = obs[0]
    assert z.kind == ZoneKind.OB and z.direction == Direction.UP
    assert (z.bottom, z.top) == (9.5, 10.3), "full_range = 該 K 的 [low, high]"
    assert z.created_at == 3, "事件時間記在突破確認根"


def test_bpr_intersection_and_priority_direction():
    from pionexbot.smc.types import Zone
    bull = Zone(ZoneKind.FVG, 10.5, 10.0, Direction.UP, created_at=5)
    bear = Zone(ZoneKind.FVG, 10.8, 10.2, Direction.DOWN, created_at=9)
    bprs = zones.detect_bprs([bull, bear])
    assert len(bprs) == 1
    z = bprs[0]
    assert (z.bottom, z.top) == (10.2, 10.5)
    assert z.direction == Direction.DOWN, "方向依後形成者"


def test_bpr_not_paired_when_far_apart_in_time():
    from pionexbot.smc.types import Zone
    bull = Zone(ZoneKind.FVG, 10.5, 10.0, Direction.UP, created_at=5)
    bear = Zone(ZoneKind.FVG, 10.8, 10.2, Direction.DOWN, created_at=500)
    assert zones.detect_bprs([bull, bear]) == [], \
        "相隔數百根的 FVG 不是同一場交戰，不得配對成 BPR"
    assert len(zones.detect_bprs([bull, bear], max_gap_bars=None)) == 1, \
        "max_gap_bars=None 應回到純數學交集（僅供測試）"


# ------------------------------------------------------------------ #
# BPR v1.1 五規則（規格 §1）
# ------------------------------------------------------------------ #
def _bpr_scene(c2_traverses: bool):
    """第一個（看漲）FVG 區 [10.0, 10.5]，第二個（看跌）FVG 的位移 K
    有沒有實際穿過第一區，由 c2_traverses 控制。"""
    from pionexbot.smc.types import Zone
    first = Zone(ZoneKind.FVG, 10.5, 10.0, Direction.UP, created_at=4)
    second = Zone(ZoneKind.FVG, 10.8, 10.2, Direction.DOWN, created_at=8)
    quiet = bar(11.0, 11.05, 10.95, 11.0)      # 不觸及任何區域
    c2 = bar(10.9, 11.0, 9.8, 10.2) if c2_traverses \
        else bar(10.9, 11.0, 10.6, 10.7)       # low 10.6 沒走完 [10.0,10.5]
    ks = [quiet] * 7 + [c2, bar(10.2, 10.25, 10.15, 10.2)]  # index 7=c2、8=c3
    return [first, second], ks


def test_bpr_requires_overlap_leg():
    zs, ks = _bpr_scene(c2_traverses=True)
    got = zones.detect_bprs(zs, ks)
    assert len(got) == 1 and got[0].kind == ZoneKind.BPR, \
        "位移 K 走完第一區 → BPR 成立"
    zs, ks = _bpr_scene(c2_traverses=False)
    assert zones.detect_bprs(zs, ks) == [], \
        "位移 K 沒穿過第一區 → 只是兩個湊巧相交的缺口，不是 BPR"


def test_bpr_dead_fvg_not_paired():
    zs, ks = _bpr_scene(c2_traverses=True)
    zs[0].removed_at = 6                       # 配對(@8)之前第一個 FVG 已失效
    assert zones.detect_bprs(zs, ks) == [], \
        "組成 BPR 的瞬間兩個 FVG 都必須還活著"


def test_bpr_width_threshold_no_exemption():
    from pionexbot.smc.types import Zone
    # 交集只有 0.02（0.2% 現價 10 → 用 0.5% 門檻擋掉）
    bull = Zone(ZoneKind.FVG, 10.02, 9.90, Direction.UP, created_at=4)
    bear = Zone(ZoneKind.FVG, 10.20, 10.00, Direction.DOWN, created_at=6)
    assert zones.detect_bprs([bull, bear], min_size_pct=0.005) == [], \
        "BPR 自身寬度必須過 min_size_pct，交集不豁免"


def test_bpr_components_must_be_displacement_grade():
    from pionexbot.smc.types import Zone
    # 兩個 0.1% 的雜訊缺口，交集 0.08% 過得了規則 4，
    # 但組件本身不到位移級（0.15%）→ 規則 6 擋下
    bull = Zone(ZoneKind.FVG, 100.10, 100.00, Direction.UP, created_at=4)
    bear = Zone(ZoneKind.FVG, 100.12, 100.02, Direction.DOWN, created_at=6)
    assert zones.detect_bprs([bull, bear]) == [], \
        "組成 BPR 的兩個 FVG 都必須是位移級"
    assert len(zones.detect_bprs([bull, bear], component_min_pct=0.0005)) == 1, \
        "門檻可調（預設跟 displacement_fvg_min_pct 同一把尺）"


def test_bpr_dedup_keeps_newer():
    from pionexbot.smc.types import Zone
    bull = Zone(ZoneKind.FVG, 10.5, 10.0, Direction.UP, created_at=4)
    bear1 = Zone(ZoneKind.FVG, 10.8, 10.2, Direction.DOWN, created_at=6)
    bear2 = Zone(ZoneKind.FVG, 10.85, 10.25, Direction.DOWN, created_at=8)
    got = zones.detect_bprs([bull, bear1, bear2])
    assert len(got) == 1 and got[0].created_at == 8, \
        "重疊過高的 BPR 只留較新者"


def test_bpr_close_through_removes_without_flip():
    zs, ks = _bpr_scene(c2_traverses=True)
    # BPR 方向依後形成者（看跌）→ 遠端是 top 10.5；收 10.8 穿越
    ks = ks + [bar(10.3, 10.9, 10.25, 10.8)]
    got = zones.detect_bprs(zs, ks)
    assert len(got) == 1
    z = got[0]
    assert z.kind == ZoneKind.BPR and z.removed_at == 9, \
        "BPR 收盤穿越遠端 → 直接移除，不翻轉"


# ------------------------------------------------------------------ #
# 結構事件位移門檻（規格 §2）
# ------------------------------------------------------------------ #
def _breakout_fixture(strong: bool):
    """swing high 12 @1；第 3 根收盤突破。strong 控制突破 K 實體大小。"""
    return [
        bar(10.0, 11.0, 9.5, 10.5),
        bar(10.5, 12.0, 10.4, 11.5),           # swing high 12 @1（conf 2）
        bar(11.5, 11.6, 10.8, 11.0),
        bar(11.0, 13.5, 11.0, 13.4) if strong  # 大實體位移
        else bar(12.0, 12.3, 11.9, 12.05),     # 實體 0.05，遠小於 0.5×ATR
    ]


def test_displacement_flag_atr_mode():
    cfg = {"structure": {"displacement_mode": "atr",
                         "min_displacement_atr": 0.5}}
    weak = structure.detect_structure(_breakout_fixture(False),
                                      left=1, right=1, smc_cfg=cfg)
    assert len(weak.events) == 1 and not weak.events[0].displacement, \
        "小實體突破 → displacement=false（但狀態機照樣翻面）"
    assert weak.trend[3] == Direction.UP, "弱勢突破仍須更新趨勢狀態"
    strong = structure.detect_structure(_breakout_fixture(True),
                                        left=1, right=1, smc_cfg=cfg)
    assert strong.events[0].displacement, "大實體突破 → displacement=true"


def test_displacement_fvg_threshold_is_independent_param():
    """位移判定的合格 FVG 門檻（0.15%）獨立於區域偵測的 fvg.min_size_pct
    （0.05%）——沿用後者會讓閘門形同虛設（2026-07-14 密度實測）。"""
    ks = [
        bar(100.0, 100.5, 99.5, 100.2),
        bar(100.2, 101.0, 100.1, 100.8),    # swing high 101 @1（conf 2）
        bar(100.8, 100.9, 99.95, 100.1),    # swing low 99.95 @2（conf 3）
        bar(100.1, 100.6, 100.0, 100.5),
        bar(100.5, 100.9, 100.45, 100.85),
        bar(100.85, 101.3, 100.68, 101.2),  # 收破 101 → BOS；腿內 FVG 寬 0.079%
    ]
    strict = structure.detect_structure(
        ks, left=1, right=1,
        smc_cfg={"structure": {"displacement_mode": "fvg"}})
    assert len(strict.events) == 1 and not strict.events[0].displacement, \
        "腿內 FVG 0.079% 低於位移門檻 0.15% → 弱事件"
    loose = structure.detect_structure(
        ks, left=1, right=1,
        smc_cfg={"structure": {"displacement_mode": "fvg",
                               "displacement_fvg_min_pct": 0.0005}})
    assert loose.events[0].displacement, \
        "調低 displacement_fvg_min_pct 應能獨立放行，與 fvg.min_size_pct 無關"


def test_weak_events_do_not_spawn_obs():
    cfg = {"structure": {"displacement_mode": "atr",
                         "min_displacement_atr": 0.5}}
    ks = _breakout_fixture(False)
    st = structure.detect_structure(ks, left=1, right=1, smc_cfg=cfg)
    assert zones.detect_obs(ks, st.events) == [], \
        "displacement=false 的事件不得生成 OB/PB"
    ks = _breakout_fixture(True)
    st = structure.detect_structure(ks, left=1, right=1, smc_cfg=cfg)
    assert len(zones.detect_obs(ks, st.events)) == 1


# ------------------------------------------------------------------ #
# 區域生命週期（規格 §3）
# ------------------------------------------------------------------ #
def test_flipped_zone_removed_on_second_close_through():
    ks = [
        bar(9.5, 10.0, 9.0, 9.8),
        bar(10.0, 10.8, 9.9, 10.7),
        bar(10.6, 11.2, 10.5, 11.0),           # FVG [10, 10.5] @2
        bar(11.0, 11.1, 10.4, 10.9),           # 觸及 → TESTED
        bar(10.8, 10.9, 9.7, 9.8),             # 收 < 10 → 翻轉 IFVG（極性轉 DOWN）
        bar(9.9, 10.8, 9.8, 10.7),             # 收 10.7 > 10.5 → 再穿 → 移除
    ]
    zs = zones.detect_fvgs(ks, min_size_pct=0.0005)
    assert len(zs) == 1
    z = zs[0]
    assert z.kind == ZoneKind.IFVG, "第一次穿越仍應翻轉"
    assert z.removed_at == 5, "翻轉後再次收盤穿越 → 移除"
    assert not z.is_active(5) and z.is_active(4)


def test_zone_ttl_removal():
    ks = [
        bar(9.5, 10.0, 9.0, 9.8),
        bar(10.0, 10.8, 9.9, 10.7),
        bar(10.6, 11.2, 10.5, 11.0),           # FVG [10, 10.5] @2
    ] + [bar(11.0, 11.1, 10.7, 11.0)] * 15     # 遠離區域的橫盤（不觸及、不成新 FVG）
    zs = zones.detect_fvgs(ks, min_size_pct=0.0005, max_age_bars=10)
    assert len(zs) == 1 and zs[0].removed_at == 13, \
        "存活超過 max_age_bars → TTL 移除"


def test_active_cap_removes_oldest():
    from pionexbot.smc.types import Zone
    zs = [Zone(ZoneKind.FVG, 10 + i, 9 + i, Direction.UP, created_at=i)
          for i in range(10)]
    zones._cap_active_per_kind(zs, max_active=8)
    removed = [z for z in zs if z.removed_at is not None]
    assert len(removed) == 2, "超過上限的數量 = 被移除的數量"
    assert {z.created_at for z in removed} == {0, 1}, "移除的必須是最舊的"


def test_ote_band_and_discount():
    lo, hi = zones.ote_band(100.0, 90.0)          # 0 在 high、1 在 low
    assert abs(lo - 92.1) < 1e-9 and abs(hi - 93.8) < 1e-9
    assert abs(zones.discount_ceiling(100.0, 90.0) - 95.0) < 1e-9


# ------------------------------------------------------------------ #
# 時段（Killzone）
# ------------------------------------------------------------------ #
def _ms(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000


def test_killzones_in_ny_winter():
    clock = sessions.SessionClock()
    assert clock.in_killzone(_ms(2026, 1, 15, 15, 0)) == "newyork"  # 10:00 EST
    assert clock.in_killzone(_ms(2026, 1, 15, 8, 30)) == "london"   # 03:30 EST
    assert clock.in_killzone(_ms(2026, 1, 16, 1, 30)) == "asia"     # 20:30 EST
    assert clock.in_killzone(_ms(2026, 1, 16, 4, 30)) == "asia"     # 23:30 EST（跨午夜段）
    assert clock.in_killzone(_ms(2026, 1, 15, 12, 0)) is None       # 07:00 EST

def test_session_pools_created_after_session_ends():
    # 倫敦時段（03:00–06:00 EST = 08:00–11:00 UTC 冬令）三根 K，之後一根時段外
    ks = [bar(10, 10.5, 9.9, 10.2, _ms(2026, 1, 15, 8, 30)),
          bar(10.2, 11.0, 10.1, 10.8, _ms(2026, 1, 15, 9, 30)),
          bar(10.8, 10.9, 10.0, 10.4, _ms(2026, 1, 15, 10, 30)),
          bar(10.4, 10.6, 10.2, 10.5, _ms(2026, 1, 15, 12, 0))]
    pools = sessions.SessionClock().session_pools(ks)
    assert len(pools) == 2
    hi = next(p for p in pools if p.side == PoolSide.BUY_SIDE)
    lo = next(p for p in pools if p.side == PoolSide.SELL_SIDE)
    assert hi.price == 11.0 and lo.price == 9.9
    assert hi.created_at == 3, "時段結束後才成為可用池（防 lookahead）"
    assert hi.source == PoolSource.SESSION


# ------------------------------------------------------------------ #
# 防重繪（repaint）：附加未來 K 線不得改變既有事件
# ------------------------------------------------------------------ #
def _walk(n, seed=42):
    """決定性偽隨機走勢（不用 random 模組，方便重現）。

    影線長度也要隨機——若固定 +0.3，轉折點會出現等高相鄰 K（上漲棒的
    high = 下一根下跌棒的 high），嚴格分形永遠不成立、測試變成空對空。"""
    ks, px, s = [], 100.0, seed

    def rand():
        nonlocal s
        s = (s * 1103515245 + 12345) % (2 ** 31)
        return s / 2 ** 31

    for i in range(n):
        o = px
        cl = px + (rand() - 0.5) * 2.0
        hi = max(o, cl) + 0.05 + rand() * 0.6
        lo = min(o, cl) - 0.05 - rand() * 0.6
        ks.append(bar(o, hi, lo, cl, i))
        px = cl
    return ks


def test_no_repaint_on_future_bars():
    full = _walk(120)
    n = 80
    st1 = structure.detect_structure(full[:n], left=3, right=3)
    st2 = structure.detect_structure(full, left=3, right=3)

    def key(e):
        return (e.kind, e.direction, e.confirmed_at, round(e.broken_level, 9))
    ev1 = {key(e) for e in st1.events}
    ev2 = {key(e) for e in st2.events if e.confirmed_at < n}
    assert ev1 == ev2, f"重繪！只出現在其一：{ev1 ^ ev2}"

    sw1 = structure.find_swings(full[:n])
    sw2 = structure.find_swings(full)
    s1 = {(s.index, s.kind, s.price) for s in sw1 if s.confirmed_at < n}
    s2 = {(s.index, s.kind, s.price) for s in sw2 if s.confirmed_at < n}
    assert s1 == s2


def test_no_repaint_fvg_and_sweeps():
    full = _walk(120, seed=7)
    n = 80
    z1 = {(z.created_at, round(z.top, 9), round(z.bottom, 9))
          for z in zones.detect_fvgs(full[:n])}
    z2 = {(z.created_at, round(z.top, 9), round(z.bottom, 9))
          for z in zones.detect_fvgs(full) if z.created_at < n}
    assert z1 == z2, "FVG 生成不得受未來 K 線影響"

    p1 = liquidity.build_pools(structure.find_swings(full[:n]))
    p2 = liquidity.build_pools(structure.find_swings(full))
    e1 = {(e.confirmed_at, round(e.wick_extreme, 9))
          for e in liquidity.detect_sweeps(full[:n], p1) if e.confirmed_at < n}
    e2 = {(e.confirmed_at, round(e.wick_extreme, 9))
          for e in liquidity.detect_sweeps(full, p2) if e.confirmed_at < n}
    assert e1 == e2, "Sweep 不得重繪"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
    print("\n全部通過" if not failed else f"\n{failed} 個測試失敗")
    sys.exit(1 if failed else 0)
