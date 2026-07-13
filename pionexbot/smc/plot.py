"""SMC 視覺化驗收：K 線 + swing / BOS / MSS / 區域 / sweep / killzone。

用途：對照講義範例圖人工驗收「程式抓到的＝課堂上畫的」。
偵測不對，後面的回測績效都沒有意義。（規格 §5.1）

效能註記：plotly 的 add_shape/add_annotation 每呼叫一次就深拷貝一次
既有物件（O(n²)），區域一多會跑不完；所以這裡把 shapes/annotations
全部先收集成 list，最後用 update_layout 一次塞入。
"""
from __future__ import annotations

from . import liquidity, ohlc, sessions, structure, zones
from .types import Direction, StructureKind, SwingKind, ZoneKind, ZoneState

ZONE_COLORS = {
    ZoneKind.OB: "rgba(46,139,87,0.25)",       # 綠：看漲訂單塊
    ZoneKind.PB: "rgba(46,139,87,0.35)",
    ZoneKind.BREAKER: "rgba(255,140,0,0.25)",  # 橘：翻轉區
    ZoneKind.FVG: "rgba(65,105,225,0.20)",     # 藍：缺口
    ZoneKind.IFVG: "rgba(186,85,211,0.25)",    # 紫：翻轉缺口
    ZoneKind.BPR: "rgba(220,20,60,0.30)",      # 紅：BPR（優先權最高）
}

KZ_COLORS = {"asia": "rgba(120,120,120,0.10)",
             "london": "rgba(255,215,0,0.08)",
             "newyork": "rgba(0,191,255,0.08)"}

MAX_ZONES_DRAWN = 150   # 只畫最近 N 個區域，避免圖太重；省略數會標在標題


def build_figure(klines, smc_cfg: dict | None = None):
    """跑全部偵測器並回傳 plotly Figure（呼叫端自行 write_html）。"""
    import plotly.graph_objects as go

    cfg = smc_cfg or {}
    swing_cfg = cfg.get("swing", {})
    left = int(swing_cfg.get("left", 3))
    right = int(swing_cfg.get("right", 3))
    n = len(klines)

    xs = list(range(n))
    fig = go.Figure(go.Candlestick(
        x=xs,
        open=[ohlc.o(k) for k in klines], high=[ohlc.h(k) for k in klines],
        low=[ohlc.l(k) for k in klines], close=[ohlc.c(k) for k in klines],
        name="K 線"))

    shapes: list[dict] = []
    ann: list[dict] = []

    # Killzone 底色（需要 K 線帶時間戳）
    clock = sessions.SessionClock(cfg.get("sessions"))
    cur = None
    start = 0
    for i, k in enumerate(klines):
        ts = ohlc.t(k)
        name = clock.in_killzone(float(ts)) if ts is not None else None
        if name != cur:
            if cur in KZ_COLORS:
                shapes.append(dict(type="rect", x0=start, x1=i,
                                   yref="paper", y0=0, y1=1,
                                   fillcolor=KZ_COLORS[cur],
                                   line_width=0, layer="below"))
            cur, start = name, i
    if cur in KZ_COLORS:
        shapes.append(dict(type="rect", x0=start, x1=n - 1,
                           yref="paper", y0=0, y1=1,
                           fillcolor=KZ_COLORS[cur], line_width=0, layer="below"))

    # Swing 標記
    swings = structure.find_swings(klines, left, right)
    for kind, sym, col in ((SwingKind.HIGH, "triangle-down", "crimson"),
                           (SwingKind.LOW, "triangle-up", "seagreen")):
        pts = [s for s in swings if s.kind == kind]
        if pts:
            fig.add_trace(go.Scatter(
                x=[s.index for s in pts], y=[s.price for s in pts],
                mode="markers", marker={"symbol": sym, "size": 9, "color": col},
                name=f"swing {kind.value.lower()}"))

    # BOS / MSS 標籤
    st = structure.detect_structure(klines, swings,
                                    break_mode=str(cfg.get("structure", {})
                                                   .get("break_mode", "close")))
    for e in st.events:
        ann.append(dict(
            x=e.confirmed_at, y=e.broken_level,
            text=f"{e.kind.value} {'↑' if e.direction == Direction.UP else '↓'}",
            showarrow=True, arrowhead=2,
            font=dict(color="green" if e.direction == Direction.UP else "red",
                      size=11 if e.kind == StructureKind.BOS else 13)))

    # 區域色塊（OB/Breaker + FVG/IFVG + BPR），只畫最近 MAX_ZONES_DRAWN 個
    fvgs = zones.detect_fvgs(klines, float(cfg.get("fvg", {})
                                           .get("min_size_pct", 0.0005)))
    obs = zones.detect_obs(klines, st.events,
                           zone_mode=str(cfg.get("ob", {}).get("zone", "full_range")))
    bprs = zones.detect_bprs(fvgs)
    all_zones = sorted(obs + fvgs + bprs, key=lambda z: z.created_at, reverse=True)
    drawn = all_zones[:MAX_ZONES_DRAWN]
    dropped = len(all_zones) - len(drawn)
    for z in drawn:
        shapes.append(dict(type="rect", x0=z.created_at,
                           x1=min(z.created_at + 30, n - 1),
                           y0=z.bottom, y1=z.top,
                           fillcolor=ZONE_COLORS.get(z.kind,
                                                     "rgba(128,128,128,0.2)"),
                           line_width=0))
        ann.append(dict(x=z.created_at, y=z.top, text=z.kind.value,
                        showarrow=False, font=dict(size=9), yshift=6))

    # Sweep 箭頭
    liq_cfg = cfg.get("liquidity", {})
    pools = liquidity.build_pools(swings,
                                  float(liq_cfg.get("eq_tolerance_pct", 0.001)))
    sweeps = liquidity.detect_sweeps(
        klines, pools, mode=str(liq_cfg.get("sweep_mode", "wick")),
        max_reclaim_bars=int(liq_cfg.get("max_reclaim_bars", 3)))
    for ev in sweeps:
        ann.append(dict(x=ev.confirmed_at, y=ev.wick_extreme, text="sweep",
                        showarrow=True, arrowhead=3,
                        font=dict(color="darkorange", size=10)))

    title = (f"SMC 偵測驗收圖（區域 {len(drawn)}/{len(all_zones)}，"
             f"省略較舊的 {dropped} 個）" if dropped
             else "SMC 偵測驗收圖")
    fig.update_layout(title=title, shapes=shapes, annotations=ann,
                      xaxis_rangeslider_visible=False, height=760)
    return fig
