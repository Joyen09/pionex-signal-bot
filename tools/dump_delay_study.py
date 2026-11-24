#!/usr/bin/env python3
"""倒貨延遲研究：「跌破當下先不賣，等 N 小時再決定」值不值得（唯讀，不碰實盤）。

要回答的問題來自實盤 2026-11-21 那筆：浮虧停損在 57,080 全賣，兩天後價格
61,242（+7.3%）。使用者的假設是「市場會製造倒貨，然後幾小時內回升」。

這支腳本把那個假設拆成兩個可檢定的問題：

  第一部分（描述性事實，不是策略）：
    九年裡每一次倒貨觸發之後，價格在 +1/2/4/8/24/48 小時分別怎麼走？
    回升的比例是多少？中位數幾 %？

  第二部分（可實作的規則）：
    把倒貨條件改成「必須連續成立 N 分鐘才真的賣」。
    條件中途消失就解除警報，不賣。逐年跑，看合計與最差年。

注意事項：
- 這是在原本固定的變體清單之外**新增**的問題，不是回頭調參數讓舊結論變好看。
  為了不變成挑選最佳值，延遲時間是預先決定的一組（0/1/2/4/8/24 小時），
  全部回報，不挑最高的那個當建議值。
- 模擬邏輯沿用 grid_range_study._sim，只加延遲，其餘完全不動。
- 延遲期間關閉快速通道，否則計時到期的那一分鐘可能被跳過。

用法：
  python tools/dump_delay_study.py --data-dir <資料目錄> --out dump_delay_out
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid_range_study as G  # noqa: E402

HORIZONS = [(60, "1 小時"), (120, "2 小時"), (240, "4 小時"),
            (480, "8 小時"), (1440, "24 小時"), (2880, "48 小時")]
DELAYS = [(0, "立刻賣（現行）"), (60, "等 1 小時"), (120, "等 2 小時"),
          (240, "等 4 小時"), (480, "等 8 小時"), (1440, "等 24 小時")]
MAX_TRIG = 20000


def _sim_delay(px, atr, adx, trend, grids, Q, rp, buf, fee, slip, mode, cooldown,
               max_loss, use_atr, atr_mult, adx_max, trend_mode, cash0,
               dump_delay, hold_breach, trig_idx):
    """grid_range_study._sim 加上「倒貨條件必須連續成立 dump_delay 分鐘」。

    dump_delay=0 時行為與 _sim 完全相同（回歸檢查會驗這件事）。
    trig_idx：輸出用，記下每次「警報首次響起」的分鐘索引；回傳值最後一項是筆數。
    """
    n = len(px)
    cash = cash0
    qty = np.zeros(grids)
    cost = np.zeros(grids)
    has = np.zeros(grids, np.bool_)
    levels = np.zeros(grids + 1)
    active = False
    lower = 0.0
    upper = 0.0
    breach_px = 0.0
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
    armed = -1          # 倒貨警報首次響起的分鐘；-1 = 沒響
    ntrig = 0
    lo_c = 1.0
    hi_c = 0.0
    for t in range(n):
        p = px[t]
        # 警報期間不走快速通道，否則計時到期那一分鐘會被跳過
        if active and armed < 0 and lo_c < p < hi_c and max_loss <= 0.0:
            if nheld > 0 and p < breach_px:
                stuck += 1
            eq = cash + inv * p
            if eq > peak:
                peak = eq
            if peak - eq > maxdd:
                maxdd = peak - eq
            continue

        want_open = False
        if not active:
            want_open = t >= wait_until
        elif nheld == 0 and (p > upper or p < lower):
            active = False
            want_open = True
        else:
            unreal = 0.0
            for i in range(grids):
                if has[i]:
                    unreal += qty[i] * (p - levels[i])
            breach = p < breach_px
            mlh = max_loss > 0.0 and unreal <= -max_loss
            # hold_breach=1 對應實盤的 grid.hold_on_breach：跌破不賣，但
            # max_loss_quote 仍然會賣。注意：跌破線只要跌 6.9% 就到，浮虧 -30
            # 要再跌 31%，所以「跌破就賣」開著時 max_loss 永遠輪不到——
            # 兩者的交互作用只有在 hold_breach=1 時才看得到。
            cond = nheld > 0 and mode != 1 and (mlh or (breach and hold_breach == 0))
            if cond:
                if armed < 0:
                    armed = t
                    if ntrig < MAX_TRIG:
                        trig_idx[ntrig] = t
                    ntrig += 1
            else:
                armed = -1          # 條件消失 → 解除警報，不賣
            if cond and (t - armed) >= dump_delay:
                proceeds = inv * p * (1 - slip) * (1 - fee)
                paid = 0.0
                for i in range(grids):
                    if has[i]:
                        paid += cost[i]
                        has[i] = False
                        qty[i] = 0.0
                        cost[i] = 0.0
                cash += proceeds
                dump_pnl += proceeds - paid
                dumps += 1
                nheld = 0
                inv = 0.0
                active = False
                armed = -1
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
                        has[i] = False
                        qty[i] = 0.0
                        cost[i] = 0.0
                        nheld -= 1
                    elif (not has[i]) and last > levels[i] and levels[i] >= p:
                        if cash >= Q:
                            q = Q * (1 - fee) / (p * (1 + slip))
                            cash -= Q
                            qty[i] = q
                            cost[i] = Q
                            has[i] = True
                            inv += q
                            nheld += 1
                        else:
                            skipped += 1
                last = p
        if want_open:
            ok = True
            a = adx[t]
            if adx_max < 900.0 and a == a and a >= adx_max:
                ok = False
            if trend_mode > 0 and not trend[t]:
                ok = False
            if ok:
                lower = p * (1 - rp)
                upper = p * (1 + rp)
                if use_atr:
                    v = atr[t]
                    if v == v and v > 0.0:
                        lower = p - atr_mult * v
                        upper = p + atr_mult * v
                step = (upper - lower) / grids
                for i in range(grids + 1):
                    levels[i] = lower + i * step
                breach_px = lower * (1 - buf)
                active = True
                last = p
        if active:
            lo_c = -1.0
            hi_c = 1e300
            on_line = False
            for i in range(grids + 1):
                lv = levels[i]
                if lv == last:
                    on_line = True
                elif lv < last:
                    if lv > lo_c:
                        lo_c = lv
                elif lv < hi_c:
                    hi_c = lv
            if breach_px == last:
                on_line = True
            elif breach_px < last:
                if breach_px > lo_c:
                    lo_c = breach_px
            elif breach_px < hi_c:
                hi_c = breach_px
            if on_line or (nheld == 0 and (last > upper or last < lower)):
                lo_c = 1.0
                hi_c = 0.0
        else:
            idle += 1
            lo_c = 1.0
            hi_c = 0.0
        eq = cash + inv * p
        if eq > peak:
            peak = eq
        if peak - eq > maxdd:
            maxdd = peak - eq
    return (cash + inv * px[n - 1] - cash0, harvest, harvest_pnl, dumps, dump_pnl,
            0, 0.0, maxdd, idle / n, stuck, skipped, ntrig)


if G.HAVE_NUMBA:
    from numba import njit
    sim_delay = njit(cache=False)(_sim_delay)
else:
    sim_delay = _sim_delay


def run(st, i, j, delay, hold_breach=0, **kw):
    p = {**G.BASE, "fee": st.fee, "slip": st.slip, **kw}
    d = st.d
    trig = np.zeros(MAX_TRIG, np.int64)
    out = sim_delay(d["px"][i:j], d["atr"][i:j], d["adx"][i:j], d[f"t{p['tn']}"][i:j],
                    p["grids"], p["Q"], p["rp"], p["buf"], p["fee"], p["slip"],
                    p["mode"], p["cooldown"], p["max_loss"], p["use_atr"],
                    p["atr_mult"], p["adx_max"], p["trend_mode"], p["cash0"],
                    delay, hold_breach, trig)
    ntrig = int(out[11])
    return out, trig[:min(ntrig, MAX_TRIG)] + i


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default="data/bitstamp-btcusd-minute-data")
    ap.add_argument("--csv")
    ap.add_argument("--out", default="dump_delay_out")
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--slip", type=float, default=0.0001)
    ap.add_argument("--rp", type=float, default=0.05,
                    help="區間幅度（預設 0.05 = 實盤現行設定）")
    ap.add_argument("--max-loss", type=float, default=0.0,
                    help="浮虧停損；實盤是 30，預設 0 = 只測跌破")
    ap.add_argument("--hold-on-breach", action="store_true",
                    help="跌破不賣（對應實盤 grid.hold_on_breach），只剩浮虧停損會賣")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    data, src = G.load_data(args)
    st = G.Study(data, args.fee, args.slip)
    px = np.asarray(data["px"], dtype=np.float64)
    n = len(px)
    i0, i1 = st.idx("2018-01-01"), st.idx(G.END_DATE)
    kw = dict(rp=args.rp, max_loss=args.max_loss)
    hb = 1 if args.hold_on_breach else 0

    L = ["# 倒貨延遲研究", "",
         f"- 資料：{src}", f"- 參數：8 格 × 12 USDT、區間 ±{args.rp:.0%}、緩衝 2%、"
         f"手續費 {args.fee:.3%}、滑價 {args.slip:.2%}"
         + (f"、浮虧停損 {args.max_loss:.0f}" if args.max_loss else "（無浮虧停損）")
         + ("、跌破不賣（hold_on_breach）" if args.hold_on_breach else "、跌破就賣"),
         "- 期間：2018-01-01 ~ " + G.END_DATE, ""]

    # ---- 第一部分：倒貨觸發後價格怎麼走（描述性事實）----
    _, trig = run(st, i0, i1, 0, hb, **kw)
    L += ["## 1. 倒貨觸發之後，價格怎麼走", "",
          f"九年共觸發 {len(trig)} 次。每次觸發當下的價格記為基準，之後各時點的漲跌：", "",
          "| 之後 | 回升比例 | 中位數 | 平均 | 10% 分位 | 90% 分位 |",
          "|---|---|---|---|---|---|"]
    rows = []
    for h, name in HORIZONS:
        ok = trig[trig + h < n]
        if len(ok) == 0:
            continue
        r = (px[ok + h] / px[ok] - 1) * 100
        rows.append({"之後": name, "樣本": len(r), "回升比例": (r > 0).mean(),
                     "中位數": np.median(r), "平均": r.mean(),
                     "p10": np.percentile(r, 10), "p90": np.percentile(r, 90)})
        L.append(f"| {name} | {(r > 0).mean():.0%} | {np.median(r):+.2f}% | "
                 f"{r.mean():+.2f}% | {np.percentile(r, 10):+.2f}% | "
                 f"{np.percentile(r, 90):+.2f}% |")
    pd.DataFrame(rows).to_csv(os.path.join(args.out, "after_trigger.csv"),
                              index=False, encoding="utf-8-sig")
    L += ["", "判讀：回升比例要明顯高於 50%、而且中位數為正，「等一下再賣」才有根據。",
          "接近 50% 就代表倒貨當下的價格沒有特別便宜，等待只是碰運氣。", ""]

    # ---- 第二部分：延遲倒貨的逐年損益 ----
    ycols = [y for y, _, _ in G.YEARS]
    L += ["## 2. 「條件連續成立 N 分鐘才賣」的逐年損益", "",
          "| 策略 | " + " | ".join(ycols) + " | 合計 | 賺錢年數 | 最差年 | 最大回撤 | 倒貨次數 |",
          "|" + "|".join(["---"] * (len(ycols) + 6)) + "|"]
    table = []
    for delay, name in DELAYS:
        yearly, dumps_tot = [], 0
        for y, a, b in G.YEARS:
            out, _ = run(st, st.idx(a), st.idx(b), delay, hb, **kw)
            yearly.append(out[0])
            dumps_tot += int(out[3])
        tot = sum(yearly)
        full, _ = run(st, i0, i1, delay, hb, **kw)
        table.append({"策略": name, **{y: v for y, v in zip(ycols, yearly)},
                      "合計": tot, "賺錢年數": sum(1 for v in yearly if v > 0),
                      "最差年": min(yearly), "最大回撤": full[7], "倒貨次數": dumps_tot})
        L.append(f"| {name} | " + " | ".join(f"{v:+.1f}" for v in yearly) +
                 f" | {tot:+.1f} | {sum(1 for v in yearly if v > 0)} | "
                 f"{min(yearly):+.1f} | {full[7]:.1f} | {dumps_tot} |")
        print(f"完成：{name}　合計 {tot:+.1f}", flush=True)
    pd.DataFrame(table).to_csv(os.path.join(args.out, "by_delay.csv"),
                               index=False, encoding="utf-8-sig")
    L += ["", "判讀：延遲要能**一致地**改善合計與最差年才算有效。",
          "若各延遲之間的差異落在雜訊範圍（彼此互有高低、沒有單調趨勢），",
          "就代表「等一下再賣」沒有真正的優勢，不要從表裡挑最高的那個。", ""]

    txt = "\n".join(L) + "\n"
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(txt)
    print("\n" + txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
