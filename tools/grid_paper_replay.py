#!/usr/bin/env python3
"""把紙上交易的成交紀錄和回測模擬器對照（唯讀，不碰實盤、不需要 API key）。

用途：紙上環境跑完 4~8 週後，拿 `export-trades` 匯出的 trades.csv，用同一段時間、
同一組參數重播模擬器，比對「買賣點」與「破網次數」是否吻合。吻合，才代表回測
描述的行為就是實際會發生的行為；不吻合，回測的結論就不能往實盤推。

這支腳本**不產生任何績效建議**。它只回答一個問題：模擬器像不像真的。
損益差幾塊錢不是重點（滑價、成交價、資料源都會造成差異），買賣的「時機與次數」
才是重點。

模擬邏輯直接沿用 tools/grid_range_study.py 的 _sim——不另寫一份，避免兩份程式
各自漂移。本腳本會先跑一次自我檢查：事件版模擬器的統計值必須和已驗收的 sim()
逐項相同，不同就中止，不會給你一份看起來很對但其實是另一套邏輯的報告。

用法：
  python tools/grid_paper_replay.py --trades paper_export/trades.csv --rp 0.15
  # 時間範圍預設從 trades.csv 的第一筆到最後一筆，也可以用 --from/--to 指定

需要 numpy、pandas，以及 tools/grid_range_study.py 和它的資料。
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid_range_study as G  # noqa: E402


# --------------------------------------------------------------------------- #
# 事件版模擬器：和 grid_range_study._sim 同一套決策順序，額外記下每一筆動作
# --------------------------------------------------------------------------- #
def replay(px, ts, grids, Q, rp, buf, fee, slip, mode, cooldown, max_loss,
           use_atr, atr, atr_mult, adx, adx_max, cash0):
    """回傳 (events, stats)。

    events：每筆 dict(ts, kind, price, level, detail)
            kind 為 OPEN / BUY / SELL / DUMP / SKIP
    stats ：和 _sim 回傳值同順序的 tuple，供自我檢查比對
    """
    n = len(px)
    cash = cash0
    qty = np.zeros(grids)
    cost = np.zeros(grids)
    has = np.zeros(grids, np.bool_)
    levels = np.zeros(grids + 1)
    active = False
    lower = upper = breach_px = 0.0
    last = px[0]
    nheld = 0
    inv = 0.0
    harvest = 0
    harvest_pnl = 0.0
    dumps = 0
    dump_pnl = 0.0
    skipped = 0
    peak = cash0
    maxdd = 0.0
    idle = 0
    stuck = 0
    wait_until = -1
    lo_c, hi_c = 1.0, 0.0
    ev = []

    for t in range(n):
        p = px[t]
        # 快速通道：價格嚴格落在相鄰兩條線之間時，這一分鐘不可能有任何動作
        if active and lo_c < p < hi_c and max_loss <= 0.0:
            if nheld > 0 and p < breach_px:
                stuck += 1
            eq = cash + inv * p
            peak = max(peak, eq)
            maxdd = max(maxdd, peak - eq)
            continue

        want_open = False
        if not active:
            want_open = t >= wait_until
        elif nheld == 0 and (p > upper or p < lower):
            active = False
            want_open = True
        else:
            unreal = sum(qty[i] * (p - levels[i]) for i in range(grids) if has[i])
            breach = p < breach_px
            mlh = max_loss > 0.0 and unreal <= -max_loss
            if nheld > 0 and mode != 1 and (breach or mlh):
                proceeds = inv * p * (1 - slip) * (1 - fee)
                paid = sum(cost[i] for i in range(grids) if has[i])
                held_n = nheld
                for i in range(grids):
                    has[i], qty[i], cost[i] = False, 0.0, 0.0
                cash += proceeds
                dump_pnl += proceeds - paid
                dumps += 1
                ev.append(dict(ts=int(ts[t]), kind="DUMP", price=float(p), level=-1,
                               detail=f"倒貨 {held_n} 格，損益 {proceeds - paid:+.2f}"
                                      f"（{'跌破' if breach else '浮虧上限'}）"))
                nheld = 0
                inv = 0.0
                active = False
                if mode == 0:
                    want_open = True
                else:
                    wait_until = t + cooldown
            else:
                if nheld > 0 and breach:
                    stuck += 1
                for i in range(grids):
                    if has[i] and p >= levels[i + 1]:
                        proceeds = qty[i] * p * (1 - slip) * (1 - fee)
                        cash += proceeds
                        harvest_pnl += proceeds - cost[i]
                        harvest += 1
                        inv -= qty[i]
                        ev.append(dict(ts=int(ts[t]), kind="SELL", price=float(p), level=i,
                                       detail=f"收割第 {i} 格，損益 {proceeds - cost[i]:+.2f}"))
                        has[i], qty[i], cost[i] = False, 0.0, 0.0
                        nheld -= 1
                    elif (not has[i]) and last > levels[i] and levels[i] >= p:
                        if cash >= Q:
                            q = Q * (1 - fee) / (p * (1 + slip))
                            cash -= Q
                            qty[i], cost[i], has[i] = q, Q, True
                            inv += q
                            nheld += 1
                            ev.append(dict(ts=int(ts[t]), kind="BUY", price=float(p),
                                           level=i, detail=f"買進第 {i} 格 {q:.8f}"))
                        else:
                            skipped += 1
                            ev.append(dict(ts=int(ts[t]), kind="SKIP", price=float(p),
                                           level=i, detail="現金不足，跳過買單"))
                last = p

        if want_open:
            ok = True
            a = adx[t]
            if adx_max < 900.0 and a == a and a >= adx_max:
                ok = False
            if ok:
                lower, upper = p * (1 - rp), p * (1 + rp)
                if use_atr:
                    v = atr[t]
                    if v == v and v > 0.0:
                        lower, upper = p - atr_mult * v, p + atr_mult * v
                step = (upper - lower) / grids
                for i in range(grids + 1):
                    levels[i] = lower + i * step
                breach_px = lower * (1 - buf)
                active = True
                last = p
                ev.append(dict(ts=int(ts[t]), kind="OPEN", price=float(p), level=-1,
                               detail=f"開網格 {lower:.2f} ~ {upper:.2f}"
                                      f"（跌破線 {breach_px:.2f}）"))

        if active:
            lo_c, hi_c = -1.0, 1e300
            on_line = False
            for i in range(grids + 1):
                lv = levels[i]
                if lv == last:
                    on_line = True
                elif lv < last:
                    lo_c = max(lo_c, lv)
                else:
                    hi_c = min(hi_c, lv)
            if breach_px == last:
                on_line = True
            elif breach_px < last:
                lo_c = max(lo_c, breach_px)
            else:
                hi_c = min(hi_c, breach_px)
            if on_line or (nheld == 0 and (last > upper or last < lower)):
                lo_c, hi_c = 1.0, 0.0
        else:
            idle += 1
            lo_c, hi_c = 1.0, 0.0

        eq = cash + inv * p
        peak = max(peak, eq)
        maxdd = max(maxdd, peak - eq)

    stats = (cash + inv * px[n - 1] - cash0, harvest, harvest_pnl, dumps, dump_pnl,
             0, 0.0, maxdd, idle / n, stuck, skipped)
    return ev, stats


# --------------------------------------------------------------------------- #
# 真實成交 vs 模擬事件
# --------------------------------------------------------------------------- #
def load_real(path: str) -> pd.DataFrame:
    """讀 export-trades 匯出的 trades.csv，只留網格相關的成交。"""
    df = pd.read_csv(path, encoding="utf-8-sig")
    need = {"ts_epoch", "side", "price", "source"}
    missing = need - set(df.columns)
    if missing:
        sys.exit(f"trades.csv 缺少欄位：{sorted(missing)}；請用 export-trades 產生的檔案")
    df = df[df.source.astype(str).str.startswith("grid")].copy()
    if df.empty:
        sys.exit("trades.csv 裡沒有 source 以 grid 開頭的成交，無從比對")
    df["ts_epoch"] = df.ts_epoch.astype(float).astype(np.int64)
    # grid:close 是「跌破倒貨」，和一般的收割賣出要分開算
    df["kind"] = np.where(df.source == "grid:close", "DUMP",
                          np.where(df.side.str.upper() == "BUY", "BUY", "SELL"))
    return df.sort_values("ts_epoch").reset_index(drop=True)


def match(real: pd.DataFrame, sim_ev: list, tol_min: int) -> tuple[pd.DataFrame, dict]:
    """把每筆真實成交配到時間最近、同方向的模擬事件（容忍 tol_min 分鐘）。

    一對一配對：一個模擬事件只能被配走一次，否則「模擬買 1 次、實際買 5 次」
    會看起來全部命中。
    """
    pool: dict[str, list] = {}
    for e in sim_ev:
        if e["kind"] in ("BUY", "SELL", "DUMP"):
            pool.setdefault(e["kind"], []).append(dict(e, used=False))
    tol = tol_min * 60
    rows = []
    for r in real.itertuples():
        best, best_d = None, None
        for e in pool.get(r.kind, []):
            if e["used"]:
                continue
            d = abs(e["ts"] - r.ts_epoch)
            if d <= tol and (best_d is None or d < best_d):
                best, best_d = e, d
        if best is not None:
            best["used"] = True
        rows.append({
            "實際時間": pd.to_datetime(r.ts_epoch, unit="s"),
            "方向": r.kind,
            "實際價": r.price,
            "模擬時間": pd.to_datetime(best["ts"], unit="s") if best else "",
            "模擬價": best["price"] if best else "",
            "差幾分鐘": round(best_d / 60, 1) if best else "",
            "價差%": round((best["price"] / r.price - 1) * 100, 3) if best else "",
            "配對": "✓" if best else "✗ 模擬沒有對應動作",
        })
    extra = [e for lst in pool.values() for e in lst if not e["used"]]
    for e in sorted(extra, key=lambda x: x["ts"]):
        rows.append({
            "實際時間": "", "方向": e["kind"], "實際價": "",
            "模擬時間": pd.to_datetime(e["ts"], unit="s"), "模擬價": e["price"],
            "差幾分鐘": "", "價差%": "", "配對": "✗ 實際沒有對應成交",
        })
    counts = {k: {"實際": int((real.kind == k).sum()),
                  "模擬": len(pool.get(k, []))} for k in ("BUY", "SELL", "DUMP")}
    matched = sum(1 for r in rows if r["配對"] == "✓")
    return pd.DataFrame(rows), {"counts": counts, "matched": matched,
                                "real_total": len(real),
                                "sim_total": sum(len(v) for v in pool.values())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--trades", required=True, help="export-trades 產生的 trades.csv")
    ap.add_argument("--data-dir", default="data/bitstamp-btcusd-minute-data")
    ap.add_argument("--csv", help="自備分鐘線 CSV（timestamp,high,low,close）")
    ap.add_argument("--out", default="replay_out", help="輸出資料夾")
    ap.add_argument("--rp", type=float, default=0.15, help="區間幅度（預設 0.15）")
    ap.add_argument("--grids", type=int, default=8)
    ap.add_argument("--quote-per-grid", type=float, default=12.0)
    ap.add_argument("--buf", type=float, default=0.02, help="跌破緩衝")
    ap.add_argument("--capital", type=float, default=108.0,
                    help="起始現金（預設 108 = 8 格 × 12 再多留一格）")
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--slip", type=float, default=0.0001)
    ap.add_argument("--tol-min", type=int, default=5,
                    help="配對容忍幾分鐘（預設 5；紙上輪詢 20 秒，但資料是 1 分鐘線）")
    ap.add_argument("--grid-meta", help="export-trades 產生的 grid_meta.csv（用來取開網格時間）")
    ap.add_argument("--from", dest="t_from", help="起（UTC）。不給就依序找 grid_meta、第一筆成交")
    ap.add_argument("--to", dest="t_to", help="迄（UTC，預設取 trades.csv 最後一筆）")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    real = load_real(args.trades)
    t0, origin = args.t_from, "--from 指定"
    if not t0 and args.grid_meta:
        m = pd.read_csv(args.grid_meta, encoding="utf-8-sig")
        col = "current_grid_created_utc"
        if col in m.columns and str(m[col].iloc[0]).strip():
            t0, origin = str(m[col].iloc[0]).strip(), f"grid_meta.csv 的 {col}"
    if not t0:
        t0, origin = str(pd.to_datetime(int(real.ts_epoch.iloc[0]), unit="s")), "第一筆成交"
        print("⚠ 起點用的是「第一筆成交時間」，不是「開網格時間」。模擬器會在這個時間點\n"
              "  才開第一個網格，比實際晚，買賣次數通常會系統性少一次。\n"
              "  請改用 --grid-meta grid_meta.csv，或用 --from 指定實際開網格的時間。",
              file=sys.stderr, flush=True)
    t1 = args.t_to or str(pd.to_datetime(int(real.ts_epoch.iloc[-1]) + 60, unit="s"))
    print(f"比對期間：{t0} ~ {t1}（UTC，起點來源：{origin}）", flush=True)

    data, src = G.load_data(args)
    st = G.Study(data, args.fee, args.slip)
    if pd.Timestamp(t1, tz="UTC").timestamp() > st.ts[-1]:
        sys.exit(f"行情資料只到 {pd.to_datetime(int(st.ts[-1]), unit='s')}，"
                 f"不足以涵蓋 {t1}。\n請更新資料："
                 f"git -C {args.data_dir} pull")
    i, j = st.idx(t0), st.idx(t1)
    if j - i < 2:
        sys.exit("比對期間太短（不足 2 分鐘）")

    cash0 = args.capital
    sl = lambda k: data[k][i:j]  # noqa: E731
    ev, stats = replay(sl("px"), st.ts[i:j], args.grids, args.quote_per_grid,
                       args.rp, args.buf, args.fee, args.slip, mode=0, cooldown=0,
                       max_loss=0.0, use_atr=False, atr=sl("atr"), atr_mult=0.0,
                       adx=sl("adx"), adx_max=1000.0, cash0=cash0)

    # ---- 自我檢查：事件版必須和已驗收的 sim() 逐項相同 ----
    ref = st.run(i, j, rp=args.rp, grids=args.grids, Q=args.quote_per_grid,
                 buf=args.buf, cash0=cash0)
    names = ("淨損益", "收割次數", "收割損益", "倒貨次數", "倒貨損益", "均線清倉次數",
             "均線清倉損益", "最大回撤", "空手比例", "套牢分鐘", "跳過買單")
    bad = [f"{n}：事件版 {a}、sim() {b}"
           for n, a, b in zip(names, stats, ref) if abs(float(a) - float(b)) > 1e-6]
    if bad:
        print("❌ 自我檢查失敗——事件版模擬器和已驗收的 sim() 不一致：", file=sys.stderr)
        for b in bad:
            print("   " + b, file=sys.stderr)
        return 2
    print("✅ 自我檢查通過：事件版與已驗收的 sim() 逐項相同", flush=True)

    pd.DataFrame(ev).assign(time=lambda d: pd.to_datetime(d.ts, unit="s")).to_csv(
        os.path.join(args.out, "sim_events.csv"), index=False, encoding="utf-8-sig")
    cmp_df, summ = match(real, ev, args.tol_min)
    cmp_df.to_csv(os.path.join(args.out, "compare.csv"), index=False, encoding="utf-8-sig")

    c = summ["counts"]
    opens = sum(1 for e in ev if e["kind"] == "OPEN")
    lines = [
        "# 紙上成交 vs 回測模擬：行為比對", "",
        f"- 期間：{t0} ~ {t1}（UTC）",
        f"- 行情：{src}",
        f"- 參數：{args.grids} 格 × {args.quote_per_grid} USDT、區間 ±{args.rp:.0%}、"
        f"緩衝 {args.buf:.0%}、手續費 {args.fee:.3%}、滑價 {args.slip:.2%}",
        f"- 配對容忍：{args.tol_min} 分鐘",
        f"- 起點來源：{origin}", "",
        "## 次數比對（這才是重點）", "",
        "| 動作 | 實際 | 模擬 | 差 |", "|---|---|---|---|",
    ]
    label = {"BUY": "買進", "SELL": "收割賣出", "DUMP": "跌破倒貨"}
    for k in ("BUY", "SELL", "DUMP"):
        lines.append(f"| {label[k]} | {c[k]['實際']} | {c[k]['模擬']} | "
                     f"{c[k]['模擬'] - c[k]['實際']:+d} |")
    # 配對率只拿「實際筆數」當分母會失真：模擬多做的那些不會被扣分。
    # 以聯集當分母（兩邊沒配到的都算沒對上），這個數字才不會自己看起來很好。
    m, rt, stt = summ["matched"], summ["real_total"], summ["sim_total"]
    union = rt + stt - m
    rate = m / union if union else 0.0
    lines += ["", f"模擬另外開了 {opens} 次網格（實際的開網格不會寫進 trades 表，無從比對）。", "",
              "## 逐筆配對", "",
              f"- 實際成交 {rt} 筆，模擬動作 {stt} 筆，配對成功 {m} 筆",
              f"- **吻合度 {rate:.0%}**（分母是聯集 {union}：實際沒配到的 {rt - m} 筆"
              f"、模擬沒配到的 {stt - m} 筆都算沒對上）",
              f"- 明細見 `{args.out}/compare.csv`、模擬事件全集見 `{args.out}/sim_events.csv`", "",
              "## 判讀", "",
              "- 看「次數」和「配對率」，不要看損益差幾塊。滑價、成交價、交易所行情與",
              "  Bitstamp 的價差都會讓損益不同，那不代表行為不吻合。",
              "- 次數差一兩次通常是邊界時機（價格剛好踩在線上、或輪詢晚了幾秒），可接受。",
              "- 次數差一倍、或倒貨次數對不上，就是行為不吻合：**回測的結論不能往實盤推**，",
              "  要先找出差在哪裡，不要改參數讓它看起來吻合。"]
    txt = "\n".join(lines) + "\n"
    with open(os.path.join(args.out, "replay_report.md"), "w", encoding="utf-8") as fh:
        fh.write(txt)
    print("\n" + txt)
    print(f"報告：{args.out}/replay_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
