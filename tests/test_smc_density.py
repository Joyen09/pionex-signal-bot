"""SMC 族群密度驗收（v1.1 §4）：單例定義對 ≠ 族群密度合理。

真實資料驗收：需要 tests/fixtures/btc_15m_1000.json（BTC 15M×1000 快照）。
產生方式（VM 上）：
    python main.py smc-stats --interval 15M --limit 1000 \
        --out tests/fixtures/btc_15m_1000.json
fixture 不存在時本檔的真實資料測試會「略過」（不算失敗），
合成資料的不變量測試照常執行。

執行：python tests/test_smc_density.py  或  python -m pytest tests/ -v
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.smc.stats import compute_stats  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "btc_15m_1000.json")

# 初始校準區間（v1.1 §4）。改動門檻值必須附 smc-plot 截圖佐證，
# 且理由只能來自密度/視覺證據——不得以回測績效為由改動（曲線擬合禁令）。
DENSITY_PER_1000_BARS_15M = {
    "swings":            (60, 140),  # 每 ~5 根一個轉折（L/R=3 的合理範圍）
    "mss_displaced_max":  8,         # displacement=true 的 MSS
    "mss_over_bos_max":   0.6,       # 皆以 displacement=true 計
    "bpr_total_max":      15,        # v1.0 實測 300+ → 修好應寥寥可數
    "bpr_active_max":     10,        # 任一時點同時活躍
    "ob_family_min_ratio": 0.5,      # (OB+PB+BREAKER) / displaced 結構事件數
}


def _fixture_stats():
    if not os.path.exists(FIXTURE):
        return None
    with open(FIXTURE, encoding="utf-8") as f:
        klines = json.load(f)
    return compute_stats(klines)


def test_density_swings_in_range():
    d = _fixture_stats()
    if d is None:
        print("    （略過：無 fixture，見檔頭產生方式）")
        return
    lo, hi = DENSITY_PER_1000_BARS_15M["swings"]
    got = d["swing_highs"] + d["swing_lows"]
    assert lo <= got * 1000 / d["bars"] <= hi, f"swing 密度 {got}/{d['bars']} 根"


def test_density_structure_events():
    d = _fixture_stats()
    if d is None:
        print("    （略過：無 fixture）")
        return
    scale = 1000 / d["bars"]
    assert d["mss_displaced"] * scale <= \
        DENSITY_PER_1000_BARS_15M["mss_displaced_max"], \
        f"強 MSS {d['mss_displaced']}（盤整裡狀態機在洗）"
    assert d["mss_over_bos"] <= DENSITY_PER_1000_BARS_15M["mss_over_bos_max"], \
        f"MSS/BOS = {d['mss_over_bos']:.2f}（結構頻繁翻面）"


def test_density_bpr_population():
    d = _fixture_stats()
    if d is None:
        print("    （略過：無 fixture）")
        return
    scale = 1000 / d["bars"]
    bpr = d["per_kind"].get("BPR", {"total": 0, "peak_active": 0})
    assert bpr["total"] * scale <= DENSITY_PER_1000_BARS_15M["bpr_total_max"], \
        f"BPR 總數 {bpr['total']}（v1.0 組合爆炸的症狀）"
    assert bpr["peak_active"] <= DENSITY_PER_1000_BARS_15M["bpr_active_max"], \
        f"BPR 峰值活躍 {bpr['peak_active']}（選區形同隨機）"


def test_density_ob_family_ratio():
    d = _fixture_stats()
    if d is None:
        print("    （略過：無 fixture）")
        return
    assert d["ob_family_ratio"] >= \
        DENSITY_PER_1000_BARS_15M["ob_family_min_ratio"], \
        f"OB 家族/強事件 = {d['ob_family_ratio']:.2f}（OB 生成斷鏈）"


# ------------------------------------------------------------------ #
# 合成資料不變量（不需 fixture，永遠執行）
# ------------------------------------------------------------------ #
def _walk(n=1000, seed=42):
    ks, px, s = [], 20000.0, seed

    def rand():
        nonlocal s
        s = (s * 1103515245 + 12345) % (2 ** 31)
        return s / 2 ** 31

    for i in range(n):
        o = px
        cl = px + (rand() - 0.5) * 60
        hi = max(o, cl) + 2 + rand() * 25
        lo = min(o, cl) - 2 - rand() * 25
        ks.append({"open": o, "high": hi, "low": lo, "close": cl,
                   "time": i * 900_000})
        px = cl
    return ks


def test_stats_runs_and_cap_invariant_holds():
    """cap 是硬上限：任何 kind 的峰值活躍數不得超過 max_active_per_kind。"""
    d = compute_stats(_walk(), {"zones": {"max_active_per_kind": 8}})
    assert d["bars"] == 1000 and d["zones_total"] >= 0
    for kind, s in d["per_kind"].items():
        assert s["peak_active"] <= 8, f"{kind} 峰值 {s['peak_active']} 超過上限"
    assert 0.0 <= d["coverage_pct_2pct_band"] <= 100.0


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
