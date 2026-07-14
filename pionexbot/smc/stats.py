"""SMC 族群密度統計（v1.1 §4）：偵測層的「健康檢查儀表板」。

單例定義對（Phase 2 已人工驗收）不代表族群密度合理——v1.0 的教訓是
10 天 BTC 15M 生出 435 個區域、其中 BPR 佔 73%、中位高度 0.07%，
選區形同隨機。本模組把密度量化，配合 test_smc_density.py 的校準區間
守住測量儀器的品質。

用法：
    python main.py smc-stats --interval 15M --limit 1000 [--symbol BTC_USDT]
"""
from __future__ import annotations

from . import liquidity, ohlc, structure, zones
from .types import StructureKind, SwingKind, ZoneKind


def compute_stats(klines, smc_cfg: dict | None = None) -> dict:
    """跑全部偵測器，回傳密度統計 dict（純函數、離線可用）。"""
    sc = smc_cfg or {}
    swing_cfg = sc.get("swing", {})
    left = int(swing_cfg.get("left", 3))
    right = int(swing_cfg.get("right", 3))
    n = len(klines)
    last = n - 1

    swings = structure.find_swings(klines, left, right)
    st = structure.detect_structure(klines, swings, smc_cfg=sc)

    def _count(kind: StructureKind, displaced: bool) -> int:
        return sum(1 for e in st.events
                   if e.kind == kind and e.displacement == displaced)

    bos_d, bos_w = _count(StructureKind.BOS, True), _count(StructureKind.BOS, False)
    mss_d, mss_w = _count(StructureKind.MSS, True), _count(StructureKind.MSS, False)

    liq = sc.get("liquidity", {})
    pools = liquidity.build_pools(swings,
                                  float(liq.get("eq_tolerance_pct", 0.001)))
    pools += liquidity.daily_pools(klines)
    sweeps = liquidity.detect_sweeps(
        klines, pools, mode=str(liq.get("sweep_mode", "wick")),
        max_reclaim_bars=int(liq.get("max_reclaim_bars", 3)))

    all_zones = zones.detect_all(klines, sc, st.events)

    per_kind: dict = {}
    for kind in ZoneKind:
        zs = [z for z in all_zones if z.kind == kind]
        if not zs:
            continue
        # 任一時點同時活躍數的峰值
        peak = max(sum(1 for z in zs if z.is_active(i)) for i in range(n))
        heights = sorted((z.top - z.bottom) /
                         ohlc.c(klines[min(z.created_at, last)])
                         for z in zs if ohlc.c(klines[min(z.created_at, last)]) > 0)
        med_h = heights[len(heights) // 2] if heights else 0.0
        per_kind[kind.value] = {
            "total": len(zs),
            "active_now": sum(1 for z in zs if z.is_active(last)),
            "peak_active": peak,
            "median_height_pct": med_h * 100,
        }

    # 活躍區域對現價 ±2% 帶的覆蓋率（區間聯集 / 帶寬）
    px = ohlc.c(klines[last])
    band_lo, band_hi = px * 0.98, px * 1.02
    segs = sorted((max(z.bottom, band_lo), min(z.top, band_hi))
                  for z in all_zones if z.is_active(last)
                  and z.top > band_lo and z.bottom < band_hi)
    covered, cur_lo, cur_hi = 0.0, None, None
    for lo, hi in segs:
        if cur_hi is None or lo > cur_hi:
            if cur_hi is not None:
                covered += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_hi is not None:
        covered += cur_hi - cur_lo
    coverage = covered / (band_hi - band_lo) if band_hi > band_lo else 0.0

    displaced_events = bos_d + mss_d
    ob_family = sum(per_kind.get(k, {}).get("total", 0)
                    for k in ("OB", "PB", "BREAKER"))
    return {
        "bars": n,
        "swing_highs": sum(1 for s in swings if s.kind == SwingKind.HIGH),
        "swing_lows": sum(1 for s in swings if s.kind == SwingKind.LOW),
        "bos_displaced": bos_d, "bos_weak": bos_w,
        "mss_displaced": mss_d, "mss_weak": mss_w,
        "mss_over_bos": (mss_d / bos_d) if bos_d else 0.0,
        "sweeps": len(sweeps),
        "zones_total": len(all_zones),
        "zones_active_now": sum(1 for z in all_zones if z.is_active(last)),
        "per_kind": per_kind,
        "coverage_pct_2pct_band": coverage * 100,
        "ob_family_ratio": (ob_family / displaced_events)
                           if displaced_events else 0.0,
    }


def format_stats(d: dict) -> str:
    """人可讀的密度表（給 smc-stats CLI）。"""
    lines = [
        f"K 線：{d['bars']} 根",
        f"swing 高/低：{d['swing_highs']} / {d['swing_lows']}",
        f"BOS：{d['bos_displaced']} 強 + {d['bos_weak']} 弱　"
        f"MSS：{d['mss_displaced']} 強 + {d['mss_weak']} 弱"
        f"（強 = displacement=true，下游只認強事件）",
        f"MSS/BOS（皆以強計）：{d['mss_over_bos']:.2f}"
        "（>0.6 代表結構頻繁翻面，可能在盤整裡洗）",
        f"sweep：{d['sweeps']}",
        f"區域總數：{d['zones_total']}（目前活躍 {d['zones_active_now']}）",
        "各 kind：總數｜此刻活躍｜峰值活躍｜中位高度%",
    ]
    for kind, s in sorted(d["per_kind"].items()):
        lines.append(f"  {kind:8s} {s['total']:4d}｜{s['active_now']:3d}｜"
                     f"{s['peak_active']:3d}｜{s['median_height_pct']:.3f}%")
    lines.append(f"活躍區域對現價 ±2% 帶覆蓋率：{d['coverage_pct_2pct_band']:.1f}%")
    lines.append(f"(OB+PB+Breaker)/強結構事件：{d['ob_family_ratio']:.2f}")
    return "\n".join(lines)
