"""族群密度驗收（v1.1 規格 §4）＋ 抽查回歸案例。

fixture：tests/fixtures/btc_15m_v11.json —— 即 smc_v11.html 那張驗收圖的
同一段 BTC 15M 1000 根（時間戳為合成等距，killzone 相關不在此驗）。

門檻為「初始校準值」，證據：同一組參數在兩個相鄰視窗（smc.html 與
smc_v11.html 的資料）皆落在門檻內。改動門檻必須附 smc-plot 佐證，
不得以回測績效為由（規格 §6 紀律）。

執行：python tests/test_smc_density.py

【簽名適配註記】同 test_smc_v11_fixes.py：可改呼叫行參數名，不得改斷言。
【本 repo 適配】位移參數走 smc_cfg dict（displacement_fvg_min_pct）。
另有 tests/test_smc_stats.py 以 compute_stats 全管線對第二份快照
（btc_15m_1000.json）做同型驗收，兩者互補。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.smc import structure, zones  # noqa: E402
from pionexbot.smc.types import StructureKind, ZoneState  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "btc_15m_v11.json")

# 位移參數（凍結候選；與 config 一致）
DISPLACEMENT = {"structure": {"displacement_mode": "fvg",
                              "displacement_fvg_min_pct": 0.0015}}

DENSITY_PER_1000_BARS_15M = {
    "swings": (60, 240),          # L/R=3 於 15M BTC 實測 ~190；上限防雜訊爆量
    "mss_displaced_max": 8,       # 實測 4（兩視窗分別為 4 / 4）
    "mss_over_bos_max": 0.6,      # 實測 0.44 / 0.57（皆以 displacement=True 計）
    "bpr_total_max": 25,          # 實測 22；有害度看活躍峰值，不看總量
    "bpr_active_peak_max": 10,    # 實測 3（任一時點同時活躍、含 TTL200）
    "ob_family_min_ratio": 0.5,   # (OB+PB+BREAKER)/位移事件，實測 ~1.0
}


def _load():
    with open(FIXTURE) as f:
        ks = json.load(f)
    assert len(ks) == 1000, "fixture 應為 1000 根"
    return ks


def _detect(ks):
    swings = structure.find_swings(ks)
    st = structure.detect_structure(ks, swings, smc_cfg=DISPLACEMENT)
    disp = [e for e in st.events if e.displacement]
    fvgs = zones.detect_fvgs(ks)                 # FVG 區域門檻維持 0.0005
    obs = zones.detect_obs(ks, disp)             # OB 只由位移事件生成
    bprs = zones.detect_bprs(fvgs, ks)           # v1.1 相鄰性規則預設
    return swings, st, disp, fvgs, obs, bprs


def test_density_gate():
    ks = _load()
    swings, st, disp, fvgs, obs, bprs = _detect(ks)
    n = len(ks)
    th = DENSITY_PER_1000_BARS_15M

    lo, hi = th["swings"]
    assert lo <= len(swings) <= hi, f"swings={len(swings)} 超出 {th['swings']}"

    bos_d = [e for e in disp if e.kind == StructureKind.BOS]
    mss_d = [e for e in disp if e.kind == StructureKind.MSS]
    mss_all = [e for e in st.events if e.kind == StructureKind.MSS]
    assert len(mss_d) <= th["mss_displaced_max"], \
        f"位移 MSS={len(mss_d)} > {th['mss_displaced_max']}（盤整假 MSS 未濾掉）"
    assert len(mss_d) < len(mss_all), \
        "位移門檻形同虛設：沒有任何 MSS 被標為弱事件"
    ratio = len(mss_d) / max(1, len(bos_d))
    assert ratio <= th["mss_over_bos_max"], \
        f"MSS/BOS（位移）={ratio:.2f} > {th['mss_over_bos_max']}"

    assert len(bprs) <= th["bpr_total_max"], \
        f"BPR 總數={len(bprs)} > {th['bpr_total_max']}（相鄰性/去重未生效）"

    ttl = 200
    death = [z.state_changed_at if z.state == ZoneState.FILLED else n
             for z in bprs]
    peak = 0
    for i in range(n):
        peak = max(peak, sum(1 for z, d in zip(bprs, death)
                             if z.created_at <= i < min(d, z.created_at + ttl)))
    assert peak <= th["bpr_active_peak_max"], \
        f"BPR 活躍峰值={peak} > {th['bpr_active_peak_max']}（生命週期未生效）"

    fam = len(obs)                                # detect_obs 已含 OB/PB→BREAKER
    fam_ratio = fam / max(1, len(disp))
    assert fam_ratio >= th["ob_family_min_ratio"], \
        f"OB家族/位移事件={fam_ratio:.2f} < {th['ob_family_min_ratio']}"


# ------------------------------------------------------------------ #
# 抽查回歸案例（真實資料、與位移參數無關）
# ------------------------------------------------------------------ #
def test_regression_bpr_904_dies_at_905():
    """smc_v11.html 上的殭屍 BPR：905 收穿遠端即應死亡，不得活到圖尾。"""
    ks = _load()
    fvgs = zones.detect_fvgs(ks)
    # 【本 repo 適配（僅呼叫行）】本 repo 在套件範圍外另加了規則 6
    # （組件 FVG 須位移級），它讓這顆殭屍根本不出生（組件 FVG@904 僅
    # 0.093% < 0.15%）。為驗證套件針對的「生命週期」契約，此處放行
    # 組件門檻讓它出生；斷言原封不動。
    bprs = zones.detect_bprs(fvgs, ks, component_min_pct=0.0)
    hits = [z for z in bprs if z.created_at == 904]
    assert hits, "前置：904 應生成 BPR（貴實作的圖上有畫，配對應成立）"
    z = hits[0]
    assert z.state == ZoneState.FILLED and z.state_changed_at == 905, \
        f"BPR@904 應在 905 收穿即死，實際 state={z.state.value} @{z.state_changed_at}"


def test_regression_zone_959_dead_by_997():
    """smc_v11.html 上的 IFVG@959：966/997 兩度被單根影線走完 → 圖尾前必須已死。"""
    ks = _load()
    _, _, _, fvgs, _, _ = _detect(ks)
    hits = [z for z in fvgs if z.created_at == 959]
    assert hits, "前置：959 應生成 FVG"
    z = hits[0]
    assert z.state == ZoneState.FILLED and (z.state_changed_at or 10**9) <= 997, \
        (f"959 區域應最遲於 997 被影線走完規則收掉，"
         f"實際 state={z.state.value} @{z.state_changed_at}")


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
