"""v1.1 修正驗收測試：對應抽查發現的三個問題。

  A. BPR 生命週期：收盤穿越「任一」遠端 → 直接 FILLED，不翻轉（殭屍 BPR@904 案例）
  B. 影線走完規則：單根 K 影線走完整區且狀態 TESTED → FILLED，
     翻轉後的第二生命（IFVG/BREAKER）同樣適用（IFVG@959 案例）
  C. 位移門檻：displacement_mode=fvg + 位移缺口 >= 0.15%（獨立於 fvg.min_size_pct）

執行：python tests/test_smc_v11_fixes.py

【簽名適配註記】若貴實作的函數參數名與此處不同（例如 detect_bprs 的
kwargs、detect_structure 的位移參數名），請只改「呼叫行」的參數名對應；
所有 assert（行為契約）不得修改。
【本 repo 適配】detect_structure 的位移參數走 smc_cfg dict：
    displacement_mode / fvg_min_size_pct →
    smc_cfg={"structure": {"displacement_mode": ..., "displacement_fvg_min_pct": ...}}
僅此對應，斷言未動。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.smc import structure, zones  # noqa: E402
from pionexbot.smc.types import (Direction, StructureKind, ZoneKind,  # noqa: E402
                                 ZoneState)

_DISP_CFG = {"structure": {"displacement_mode": "fvg",
                           "displacement_fvg_min_pct": 0.0015}}


def bar(o, h, l, c, t=None):  # noqa: E741
    return {"open": o, "high": h, "low": l, "close": c, "time": t}


def _bpr_base():
    """FVG+ [10.0,10.4]@2，之後反向位移穿過 → FVG- [10.05,10.35]@5 → BPR@5。"""
    return [
        bar(9.8, 10.0, 9.7, 9.9),        # bull c1
        bar(9.9, 10.9, 9.85, 10.8),      # bull c2 位移
        bar(10.6, 10.9, 10.4, 10.7),     # bull c3 → FVG+ [10.0,10.4]
        bar(10.7, 10.8, 10.35, 10.5),    # bear c1
        bar(10.5, 10.55, 9.6, 9.7),      # bear c2 位移（涵蓋交集）
        bar(9.7, 10.05, 9.6, 9.9),       # bear c3 → FVG- [10.05,10.35] → BPR
    ]


def _bpr_at(ks, created_at=5):
    fvgs = zones.detect_fvgs(ks, min_size_pct=0.0001)
    bprs = zones.detect_bprs(fvgs, ks)
    hits = [z for z in bprs if z.created_at == created_at]
    assert hits, "前置條件失敗：BPR 應在 index 5 生成"
    return hits[0]


# ------------------------------------------------------------------ #
# A. BPR：收穿任一遠端 → 直接死，不翻轉
# ------------------------------------------------------------------ #
def test_bpr_stays_fresh_without_close_through():
    z = _bpr_at(_bpr_base())
    assert z.state == ZoneState.FRESH, "對照組：無收穿時 BPR 應為 FRESH"


def test_bpr_dies_on_close_above_top():
    ks = _bpr_base() + [bar(9.9, 10.6, 9.85, 10.5)]   # 收 10.5 > top 10.35
    z = _bpr_at(ks)
    assert z.state == ZoneState.FILLED, "收盤穿越上遠端 → BPR 必須 FILLED"
    assert z.kind == ZoneKind.BPR, "BPR 不得翻轉成其他 kind"
    assert z.state_changed_at == 6


def test_bpr_dies_on_close_below_bottom():
    ks = _bpr_base() + [bar(9.9, 10.0, 9.5, 9.6)]     # 收 9.6 < bottom 10.05
    z = _bpr_at(ks)
    assert z.state == ZoneState.FILLED, "收盤穿越下遠端 → BPR 必須 FILLED"
    assert z.state_changed_at == 6


# ------------------------------------------------------------------ #
# B. 影線走完整區（TESTED）→ FILLED；第二生命同樣適用
# ------------------------------------------------------------------ #
def _fvg_base():
    """FVG+ [10.0,10.4]@2。"""
    return [
        bar(9.8, 10.0, 9.7, 9.9),
        bar(9.9, 10.9, 9.85, 10.8),
        bar(10.6, 10.9, 10.4, 10.7),
    ]


def test_wick_traversal_kills_tested_fvg():
    ks = _fvg_base() + [
        bar(10.7, 10.75, 10.3, 10.6),    # 觸及（未收穿、未走完）→ TESTED
        bar(10.55, 10.6, 9.9, 10.15),    # 影線 9.9 走完整區、收 10.15 未收穿
    ]
    z = [x for x in zones.detect_fvgs(ks, min_size_pct=0.0001)
         if x.created_at == 2][0]
    assert z.flips == 0 and z.kind == ZoneKind.FVG
    assert z.state == ZoneState.FILLED, \
        "TESTED 後被單根影線走完整區 → 必須 FILLED（v1.0 規則，v1.1 保留）"
    assert z.state_changed_at == 4


def test_wick_traversal_kills_second_life_ifvg():
    ks = _fvg_base() + [
        bar(10.5, 10.55, 9.6, 9.7),      # 收 9.7 < 10.0 → 翻轉為 IFVG（第二生命）
        bar(9.8, 10.2, 9.75, 10.1),      # 觸及 → TESTED（新方向 DOWN）
        bar(10.05, 10.45, 9.95, 10.2),   # 影線 10.45 走完整區、收 10.2 未收穿
    ]
    z = [x for x in zones.detect_fvgs(ks, min_size_pct=0.0001)
         if x.created_at == 2][0]
    assert z.kind == ZoneKind.IFVG and z.flips == 1, "前置：應已翻轉一次"
    assert z.state == ZoneState.FILLED, \
        "第二生命（IFVG/BREAKER）同樣適用影線走完規則"


# ------------------------------------------------------------------ #
# C. 位移門檻：fvg 模式，位移缺口 >= 0.15%（獨立參數）
# ------------------------------------------------------------------ #
def test_displacement_false_for_grind_break_without_gap():
    ks = [
        bar(100.2, 100.6, 99.8, 100.0),
        bar(100.0, 101.0, 99.9, 100.8),     # swing high 101 @1（left=right=1 → conf@2）
        bar(100.8, 100.9, 100.3, 100.5),
        bar(100.5, 100.9, 100.2, 100.7),
        bar(100.7, 101.15, 100.6, 101.05),  # 收 101.05 > 101 → BOS，但推進段無缺口
    ]
    st = structure.detect_structure(ks, left=1, right=1, smc_cfg=_DISP_CFG)
    ev = [e for e in st.events
          if e.kind == StructureKind.BOS and e.confirmed_at == 4]
    assert ev, "事件本身必須照發（狀態機不受位移門檻影響）"
    assert ev[0].displacement is False, \
        "推進段沒有 >=0.15% 缺口 → displacement 必須為 False"


def test_displacement_true_when_leg_leaves_gap():
    ks = [
        bar(100.2, 100.6, 99.8, 100.0),
        bar(100.0, 101.0, 99.9, 100.8),     # swing high 101 @1
        bar(100.8, 100.9, 100.3, 100.5),
        bar(100.5, 101.6, 100.45, 100.95),  # 位移 c2（收盤未破 101）
        bar(101.0, 101.7, 101.1, 101.6),    # c3 low 101.1 > high(c1)=100.9 → 缺口 0.2
    ]                                        # 收 101.6 > 101 → BOS@4，推進段含缺口
    st = structure.detect_structure(ks, left=1, right=1, smc_cfg=_DISP_CFG)
    ev = [e for e in st.events
          if e.kind == StructureKind.BOS and e.confirmed_at == 4]
    assert ev and ev[0].displacement is True, \
        "推進段留下 >=0.15% 缺口 → displacement 必須為 True"


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
