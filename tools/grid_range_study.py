#!/usr/bin/env python3
"""網格區間寬度回測（獨立腳本，唯讀，不碰實盤、不下單、不需要 API key）。

目的：驗證「現行 ±5% 區間太窄；放寬到 ±10%~±15% 是否一致地比較好」。

模擬器逐分鐘回放收盤價，決策順序照 pionexbot/sources/grid_runner.py 的 run_once：
  1. 沒有網格 → 以現價為中心開新網格（受 ADX / 均線閘門限制）
  2. 空手且價格出界 → 立刻重新定位（不等緩衝）
  3. 價格 < 下緣 ×(1-buffer)（或浮虧達 max_loss）→ 全部市價賣出，再重開
  4. 持有第 i 格且價格 >= 第 i+1 條線 → 賣出；未持有且價格由上往下穿過第 i 條線 → 買入
成交價用該分鐘收盤價，加上手續費與滑價。

用法：
  python grid_range_study.py                 # 第一次會 git clone 資料（約 250MB）
  python grid_range_study.py --verify        # 跑完後和內建參考值比對
  python grid_range_study.py --csv my.csv    # 改用自備分鐘線（欄位 timestamp,high,low,close；timestamp 為 UTC 秒）
  python grid_range_study.py --quick         # 跳過滾動視窗（較快）

需要 numpy、pandas；有 numba 會快很多（沒有也能跑，約數分鐘）。
"""
from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import time

import numpy as np
import pandas as pd

try:
    from numba import njit
    HAVE_NUMBA = os.environ.get("NUMBA_DISABLE_JIT", "0") != "1"
except Exception:  # noqa: BLE001
    HAVE_NUMBA = False

DATA_REPO = "https://github.com/ff137/bitstamp-btcusd-minute-data"
HIST_FILE = "data/historical/btcusd_bitstamp_1min_2012-2025.csv.gz"
LATEST_FILE = "data/updates/btcusd_bitstamp_1min_latest.csv"
END_DATE = "2026-10-07"          # 固定結束日，結果才可重現
YEARS = [(str(y), f"{y}-01-01", f"{y + 1}-01-01") for y in range(2018, 2026)] + [("2026", "2026-01-01", END_DATE)]
LIVE_WINDOW = ("2026-07-01 15:31", "2026-10-07 02:06")   # 實盤匯出檔涵蓋的期間
LIVE_ACTUAL = "收割 92 次、倒貨 1 次（-2.72）、淨損益 +6.6 ~ +10.7（差在帳上零頭是否還在）"

# 實盤現行參數（grid_meta.csv：8 格、每格 12、fixed ±5%、緩衝 2%、跌破重開、ADX 關）
BASE = dict(grids=8, Q=12.0, rp=0.05, buf=0.02, fee=0.0005, slip=0.0001, mode=0, cooldown=0,
            max_loss=0.0, use_atr=False, atr_mult=6.0, adx_max=999.0, trend_mode=0, tn=200, cash0=108.0)
STAKE = 96.0   # 對照組投入金額（= 8 格 × 12）


# --------------------------------------------------------------------------- 模擬器
def _sim(px, atr, adx, trend, grids, Q, rp, buf, fee, slip, mode, cooldown, max_loss,
         use_atr, atr_mult, adx_max, trend_mode, cash0):
    """mode: 0=跌破就全賣並重開（現行） 1=跌破不賣、抱著等 2=全賣後等 cooldown 分鐘再開
    trend_mode: 0=關 1=均線下不開新網格 2=跌破均線就清倉停機
    回傳 (淨損益, 收割次數, 收割損益, 倒貨次數, 倒貨損益, 均線清倉次數, 均線清倉損益,
          最大回撤, 空手比例, 套牢分鐘數, 因現金不足跳過的買單)"""
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
    texits = 0
    texit_pnl = 0.0
    skipped = 0
    peak = cash0
    maxdd = 0.0
    idle = 0
    stuck = 0
    wait_until = -1
    lo_c = 1.0     # 快速通道：價格嚴格落在 (lo_c, hi_c) 內時，這一分鐘不可能有任何動作
    hi_c = 0.0
    for t in range(n):
        p = px[t]
        if active and lo_c < p < hi_c and max_loss <= 0.0 and (trend_mode != 2 or trend[t]):
            if nheld > 0 and p < breach_px:
                stuck += 1
            eq = cash + inv * p
            if eq > peak:
                peak = eq
            if peak - eq > maxdd:
                maxdd = peak - eq
            continue

        want_open = False
        if active and trend_mode == 2 and not trend[t]:
            if nheld > 0:
                proceeds = inv * p * (1 - slip) * (1 - fee)
                paid = 0.0
                for i in range(grids):
                    if has[i]:
                        paid += cost[i]
                        has[i] = False
                        qty[i] = 0.0
                        cost[i] = 0.0
                cash += proceeds
                texit_pnl += proceeds - paid
                texits += 1
                nheld = 0
                inv = 0.0
            active = False
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
            if nheld > 0 and mode != 1 and (breach or mlh):
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
                # 與 grid_runner._grid_bounds 相同的算式（浮點誤差會影響中心那一格是否立刻買進）
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
        # 重新計算快速通道的上下界（以 last 所在的格子為準；剛好踩在線上就不走快速通道）
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
            # 空手且已在區間外：下一分鐘要走完整流程做「重新定位」，不能走快速通道
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
    return (cash + inv * px[n - 1] - cash0, harvest, harvest_pnl, dumps, dump_pnl, texits, texit_pnl,
            maxdd, idle / n, stuck, skipped)


def _trend_hold(px, trend, stake, fee, slip):
    """對照組：站上均線就把 stake 全換成 BTC，跌破就全賣回現金。"""
    cash = stake
    q = 0.0
    for t in range(len(px)):
        p = px[t]
        if trend[t] and q == 0.0:
            q = cash * (1 - fee) / (p * (1 + slip))
            cash = 0.0
        elif (not trend[t]) and q > 0.0:
            cash = q * p * (1 - slip) * (1 - fee)
            q = 0.0
    return cash + q * px[len(px) - 1] - stake


if HAVE_NUMBA:
    sim = njit(cache=False)(_sim)
    trend_hold = njit(cache=False)(_trend_hold)
else:
    sim, trend_hold = _sim, _trend_hold


# --------------------------------------------------------------------------- 資料
def _wilder_atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _adx(h, l, c, n=14):
    """與 pionexbot/strategy/indicators.py 的 adx 相同算法。"""
    up, dn = h.diff(), -l.diff()
    pdm = ((up > dn) & (up > 0)) * up
    mdm = ((dn > up) & (dn > 0)) * dn
    a = _wilder_atr(h, l, c, n)
    pdi = 100 * (pdm.ewm(alpha=1 / n, adjust=False).mean() / a)
    mdi = 100 * (mdm.ewm(alpha=1 / n, adjust=False).mean() / a)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def load_data(args):
    cols = ["timestamp", "high", "low", "close"]
    if args.csv:
        d = pd.read_csv(args.csv, usecols=cols)
        src = args.csv
    else:
        hist, latest = os.path.join(args.data_dir, HIST_FILE), os.path.join(args.data_dir, LATEST_FILE)
        if not (os.path.exists(hist) and os.path.exists(latest)):
            print(f"下載資料：git clone --depth 1 {DATA_REPO} {args.data_dir}（約 250MB）", flush=True)
            subprocess.run(["git", "clone", "--depth", "1", DATA_REPO, args.data_dir], check=True)
        d = pd.concat([pd.read_csv(hist, usecols=cols), pd.read_csv(latest, usecols=cols)])
        src = "Bitstamp BTC/USD 1 分鐘線（ff137/bitstamp-btcusd-minute-data）"
    d = d.drop_duplicates("timestamp").sort_values("timestamp")
    d = d[d.timestamp >= pd.Timestamp("2017-01-01", tz="UTC").timestamp()].reset_index(drop=True)
    ts = d.timestamp.values.astype(np.int64)
    gaps = np.diff(ts)
    if (gaps != 60).any():
        print(f"⚠ 資料有 {(gaps != 60).sum()} 處不是連續 1 分鐘（最大間隔 {gaps.max()} 秒），結果僅供參考")
    need_end = pd.Timestamp(END_DATE, tz="UTC").timestamp()
    if ts[-1] < need_end - 60:
        sys.exit(f"資料只到 {pd.to_datetime(ts[-1], unit='s')}，不足 {END_DATE}；請更新資料或自備 --csv")
    # 小時級 ATR/ADX：每分鐘只用「已收完的上一根小時 K」，避免偷看未來
    hb = ts // 3600
    H = d.groupby(hb).agg(h=("high", "max"), l=("low", "min"), c=("close", "last"))
    H["atr"], H["adx"] = _wilder_atr(H.h, H.l, H.c), _adx(H.h, H.l, H.c)
    Hs = H[["atr", "adx"]].shift(1)
    # 日線均線：每分鐘只用「已收完的上一根日 K」
    db = ts // 86400
    D = d.groupby(db).agg(c=("close", "last"))
    trend = {n: (D.c > D.c.rolling(n).mean()).shift(1).reindex(db).fillna(False).values.astype(np.bool_)
             for n in (100, 200)}
    m = ts >= pd.Timestamp("2017-12-25", tz="UTC").timestamp()
    out = dict(ts=ts[m], px=d.close.values[m].astype(np.float64), atr=Hs.atr.reindex(hb).values[m],
               adx=Hs.adx.reindex(hb).values[m], t100=trend[100][m], t200=trend[200][m])
    if not HAVE_NUMBA:   # 純 Python 用 list 取值比 ndarray 快
        out = {k: (v if k == "ts" else v.tolist()) for k, v in out.items()}
    return out, src


class Study:
    def __init__(self, data, fee, slip):
        self.d, self.fee, self.slip = data, fee, slip
        self.ts = data["ts"]

    def idx(self, when):
        return int(np.searchsorted(self.ts, pd.Timestamp(when, tz="UTC").timestamp()))

    def run(self, i, j, **kw):
        p = {**BASE, "fee": self.fee, "slip": self.slip, **kw}
        d = self.d
        return sim(d["px"][i:j], d["atr"][i:j], d["adx"][i:j], d[f"t{p['tn']}"][i:j], p["grids"], p["Q"], p["rp"],
                   p["buf"], p["fee"], p["slip"], p["mode"], p["cooldown"], p["max_loss"], p["use_atr"],
                   p["atr_mult"], p["adx_max"], p["trend_mode"], p["cash0"])

    def by_year(self, name, **kw):
        r = {"策略": name}
        hv = dm = 0
        dd = []
        for y, a, b in YEARS:
            res = self.run(self.idx(a), self.idx(b), **kw)
            r[y] = res[0]
            hv += res[1]
            dm += res[3] + res[5]
            dd.append(res[7])
        vals = [r[y] for y, _, _ in YEARS]
        r.update({"合計": sum(vals), "賺錢年數": sum(v > 0 for v in vals), "最差年": min(vals), "最大回撤": max(dd),
                  "收割": hv, "認賠": dm, "收割/認賠": hv / dm if dm else float("nan")})
        return r

    def bench(self, name, tn=None):
        r = {"策略": name}
        px = self.d["px"]
        for y, a, b in YEARS:
            i, j = self.idx(a), self.idx(b)
            if tn is None:
                r[y] = STAKE * (px[j - 1] / px[i] - 1)
            else:
                r[y] = trend_hold(px[i:j], self.d[f"t{tn}"][i:j], STAKE, self.fee, self.slip)
        vals = [r[y] for y, _, _ in YEARS]
        r.update({"合計": sum(vals), "賺錢年數": sum(v > 0 for v in vals), "最差年": min(vals)})
        return r


GROUPS = {
    "1. 區間寬度（其餘同現行）": [
        ("±5%（現行）", {}), ("±7.5%", dict(rp=0.075)), ("±10%", dict(rp=0.10)), ("±15%", dict(rp=0.15)),
        ("±20%", dict(rp=0.20)), ("±30%", dict(rp=0.30))],
    "2. 跌破處理方式（區間 ±5%）": [
        ("跌破不賣、抱著等", dict(mode=1)), ("全賣後等 24 小時再開", dict(mode=2, cooldown=1440)),
        ("全賣後等 7 天再開", dict(mode=2, cooldown=10080)), ("緩衝放寬到 5%", dict(buf=0.05)),
        ("緩衝放寬到 10%", dict(buf=0.10)), ("浮虧達 3 就停損", dict(max_loss=3.0))],
    "3. ATR 動態區間 × ADX 過濾": [
        (f"ATR×{am:.0f}" + ("" if ax > 900 else f" + ADX<{ax:.0f}"), dict(use_atr=True, atr_mult=am, adx_max=ax))
        for am in (4.0, 6.0, 8.0) for ax in (999.0, 30.0, 20.0)
    ] + [("±5% + ADX<30", dict(adx_max=30.0)), ("±5% + ADX<20", dict(adx_max=20.0))],
    "4. 200 日均線閘門": [
        ("±5% + 跌破 200 日線清倉停機", dict(trend_mode=2)),
        ("跌破不賣 + 跌破 200 日線清倉", dict(mode=1, trend_mode=2))],
    "5. 手續費加倍（單邊 0.1%）": [
        ("±5% 手續費 0.1%", dict(fee=0.001)), ("±15% 手續費 0.1%", dict(rp=0.15, fee=0.001))],
}

# 參考值：2026-10-08 用 Bitstamp 資料、fee 0.05%、slip 0.01% 跑出的「九年合計」
REFERENCE = {
    '±5%（現行）': -118.68,
    '±7.5%': 23.41,
    '±10%': 53.64,
    '±15%': 93.15,
    '±20%': 75.84,
    '±30%': 68.61,
    '跌破不賣、抱著等': 116.61,
    '全賣後等 24 小時再開': -103.12,
    '全賣後等 7 天再開': -107.34,
    '緩衝放寬到 5%': 16.64,
    '緩衝放寬到 10%': 0.61,
    '浮虧達 3 就停損': -117.85,
    'ATR×4': 4.41,
    'ATR×4 + ADX<30': -58.03,
    'ATR×4 + ADX<20': -104.66,
    'ATR×6': 15.30,
    'ATR×6 + ADX<30': -21.19,
    'ATR×6 + ADX<20': -16.55,
    'ATR×8': 70.82,
    'ATR×8 + ADX<30': 39.30,
    'ATR×8 + ADX<20': 50.60,
    '±5% + ADX<30': -140.92,
    '±5% + ADX<20': -115.60,
    '±5% + 跌破 200 日線清倉停機': -33.39,
    '跌破不賣 + 跌破 200 日線清倉': 111.75,
    '±5% 手續費 0.1%': -241.44,
    '±15% 手續費 0.1%': 73.72,
    '買進持有 96': 565.01,
    '站上 100 日線才持有 96': 463.85,
    '站上 200 日線才持有 96': 322.66,
    '滾動中位數:±5%（現行）': -1.19,
    '滾動中位數:±15%': 2.44,
    '滾動中位數:跌破不賣、抱著等': 2.42,
    '滾動中位數:買進持有 96': 2.09,
    '校準:收割': 91.00,
    '校準:倒貨': 1.00,
    '校準:淨損益': 10.72,
}


def fmt_table(rows, cols):
    df = pd.DataFrame(rows)[cols]
    out = df.copy()
    for c in cols[1:]:
        if c in ("賺錢年數", "收割", "認賠"):
            out[c] = df[c].map(lambda v: f"{int(v)}")
        elif c == "收割/認賠":
            out[c] = df[c].map(lambda v: "—" if v != v else f"{v:.0f}")
        elif c == "最大回撤":
            out[c] = df[c].map(lambda v: f"{v:.1f}")
        else:
            out[c] = df[c].map(lambda v: f"{v:+.1f}")
    head = "| " + " | ".join(cols) + " |\n|" + "|".join(["---"] * len(cols)) + "|\n"
    return head + "\n".join("| " + " | ".join(r) + " |" for r in out.values.tolist())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default="data/bitstamp-btcusd-minute-data", help="資料 repo 的位置（不存在會自動 clone）")
    ap.add_argument("--csv", help="自備分鐘線 CSV（timestamp,high,low,close）")
    ap.add_argument("--out", default="grid_study_out", help="輸出資料夾")
    ap.add_argument("--fee", type=float, default=0.0005, help="單邊手續費率（預設 0.05%%，請換成實際費率再跑一次）")
    ap.add_argument("--slip", type=float, default=0.0001, help="單邊滑價（預設 0.01%%）")
    ap.add_argument("--quick", action="store_true", help="跳過滾動 97 天視窗")
    ap.add_argument("--verify", action="store_true", help="和內建參考值比對（只在預設資料與預設費率下有意義）")
    ap.add_argument("--print-reference", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    t0 = time.time()
    print(f"numba：{'有' if HAVE_NUMBA else '沒有（用純 Python，會慢一些）'}", flush=True)
    data, src = load_data(args)
    st = Study(data, args.fee, args.slip)
    px = data["px"]
    L = ["# 網格區間寬度回測報告", "",
         f"- 資料：{src}，{pd.to_datetime(int(st.ts[0]), unit='s')} ~ {END_DATE}",
         f"- 參數：8 格 × 12 USDT、緩衝 2%、每年以 108 USDT 重新開始；手續費單邊 {args.fee:.2%}、滑價 {args.slip:.2%}",
         "- 單位：USDT。年度欄位是該年淨損益（含期末未平倉市值）。", ""]

    # ---- 0. 校準：實盤期間
    i, j = st.idx(LIVE_WINDOW[0]), st.idx(LIVE_WINDOW[1])
    c = st.run(i, j)
    calib = f"模擬：收割 {c[1]} 次、倒貨 {c[3]} 次（{c[4]:+.2f}）、淨損益 {c[0]:+.2f}"
    L += ["## 0. 校準（實盤期間 2026-07-01 ~ 2026-10-07）", "", f"- 實際：{LIVE_ACTUAL}", f"- {calib}", ""]
    print("校準｜實際：" + LIVE_ACTUAL + "\n校準｜" + calib, flush=True)

    # ---- 1~5. 各組逐年
    ycols = [y for y, _, _ in YEARS]
    allrows, totals = [], {}
    for g, variants in GROUPS.items():
        rows = [st.by_year(n, **kw) for n, kw in variants]
        for r in rows:
            totals[r["策略"]] = r["合計"]
            allrows.append({"組別": g, **r})
        L += [f"## {g}", "", fmt_table(rows, ["策略"] + ycols + ["合計", "賺錢年數", "最差年", "最大回撤", "收割/認賠"]), ""]
        print(f"完成 {g}（{time.time() - t0:.0f}s）", flush=True)
    bench = [st.bench("買進持有 96"), st.bench("站上 100 日線才持有 96", 100), st.bench("站上 200 日線才持有 96", 200)]
    for r in bench:
        totals[r["策略"]] = r["合計"]
        allrows.append({"組別": "對照組", **r})
    bh = "、".join(f"{y} {px[st.idx(b) - 1] / px[st.idx(a)] - 1:+.0%}" for y, a, b in YEARS)
    L += ["## 對照組（不跑網格）", "", fmt_table(bench, ["策略"] + ycols + ["合計", "賺錢年數", "最差年"]), "", f"BTC 年漲跌：{bh}", ""]
    pd.DataFrame(allrows).to_csv(os.path.join(args.out, "by_year.csv"), index=False, encoding="utf-8-sig")

    # ---- 6. 滾動 97 天視窗
    if not args.quick:
        W, i0 = 97 * 1440, st.idx("2018-01-01")
        picks = [("±5%（現行）", {}), ("±15%", dict(rp=0.15)), ("跌破不賣、抱著等", dict(mode=1))]
        res = {n: [] for n, _ in picks}
        res["買進持有 96"] = []
        starts = []
        for s in range(i0, st.idx(END_DATE) - W, 7 * 1440):
            starts.append(pd.to_datetime(int(st.ts[s]), unit="s"))
            for n, kw in picks:
                res[n].append(st.run(s, s + W, **kw)[0])
            res["買進持有 96"].append(STAKE * (px[s + W - 1] / px[s] - 1))
        w = pd.DataFrame(res, index=starts)
        w.to_csv(os.path.join(args.out, "rolling97.csv"), encoding="utf-8-sig")
        summ = pd.DataFrame({"賺錢比例": (w > 0).mean().map(lambda v: f"{v:.0%}"), "中位數": w.median().map(lambda v: f"{v:+.1f}"),
                             "平均": w.mean().map(lambda v: f"{v:+.1f}"), "最差": w.min().map(lambda v: f"{v:+.1f}"),
                             "10% 分位": w.quantile(0.1).map(lambda v: f"{v:+.1f}"), "90% 分位": w.quantile(0.9).map(lambda v: f"{v:+.1f}")})
        pct = (w["±5%（現行）"] < c[0]).mean()
        L += [f"## 6. 滾動 97 天視窗（共 {len(w)} 個，每 7 天起一個）", "",
              "| 策略 | " + " | ".join(summ.columns) + " |\n|" + "|".join(["---"] * (len(summ.columns) + 1)) + "|\n"
              + "\n".join(f"| {k} | " + " | ".join(v) + " |" for k, v in zip(summ.index, summ.values.tolist())), "",
              f"實盤那 97 天的模擬結果（{c[0]:+.1f}）在「±5%（現行）」分布中排第 {pct:.0%} 百分位。", ""]
        for k in summ.index:
            totals[f"滾動中位數:{k}"] = float(w[k].median())
        print(f"完成滾動視窗（{time.time() - t0:.0f}s）", flush=True)

    report = os.path.join(args.out, "report.md")
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n" + "\n".join(L))
    print(f"報告：{report}　明細：{args.out}/by_year.csv" + ("" if args.quick else f"、{args.out}/rolling97.csv"))
    totals["校準:收割"], totals["校準:倒貨"], totals["校準:淨損益"] = float(c[1]), float(c[3]), float(c[0])

    if args.print_reference:
        print("REFERENCE = {")
        for k, v in totals.items():
            print(f"    {k!r}: {v:.2f},")
        print("}")
    if args.verify:
        bad = [(k, v, totals.get(k)) for k, v in REFERENCE.items()
               if totals.get(k) is None or not math.isclose(totals[k], v, abs_tol=0.15)]
        missing_ok = args.quick
        bad = [b for b in bad if not (missing_ok and b[0].startswith("滾動"))]
        if bad:
            print(f"\n❌ 驗證失敗 {len(bad)} 項（名稱 / 參考值 / 本次）：")
            for k, v, got in bad:
                print(f"   {k}: {v:+.2f} / {got if got is None else format(got, '+.2f')}")
            return 1
        print(f"\n✅ 驗證通過：{len(REFERENCE) - sum(k.startswith('滾動') for k in REFERENCE) * missing_ok} 項都在 ±0.15 內")
    return 0


if __name__ == "__main__":
    sys.exit(main())
