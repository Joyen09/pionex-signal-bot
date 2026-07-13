"""Phase 3 ict2022 測試：setup 流水線、MTF 防 lookahead、回測煙霧測試。

執行：python tests/test_ict2022.py  或  python -m pytest tests/ -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.backtest_ict import backtest_ict2022  # noqa: E402
from pionexbot.smc.mtf import MtfView, close_time, interval_ms  # noqa: E402
from pionexbot.strategy.ict2022 import find_setup  # noqa: E402


def bar(o, h, l, c, t=None):  # noqa: E741
    return {"open": o, "high": h, "low": l, "close": c, "time": t}


# ------------------------------------------------------------------ #
# MTF：防 lookahead
# ------------------------------------------------------------------ #
def test_interval_ms_parsing():
    assert interval_ms("5M") == 300_000
    assert interval_ms("60M") == 3_600_000
    assert interval_ms("4H") == 14_400_000
    assert interval_ms("1D") == 86_400_000


def test_mtf_view_only_returns_closed_candles():
    # trigger 15M：開盤時間 0, 900k, 1800k...
    trig = [bar(1, 1, 1, 1, i * 900_000) for i in range(10)]
    view = MtfView(trig, "15M")
    # entry 5M 第 3 根（開盤 600k）收盤於 900k → 只有 trigger 第 0 根（收盤 900k）可見
    got = view.upto(900_000)
    assert len(got) == 1
    # entry 收盤 899_999 → trigger 第 0 根還沒收 → 什麼都看不到
    assert MtfView(trig, "15M").upto(899_999) == []
    # 收盤時間必須 <= entry 收盤（逐根驗證不含未來 K）
    got = view.upto(4_500_000)
    assert all(close_time(k, "15M") <= 4_500_000 for k in got)
    assert len(got) == 5


# ------------------------------------------------------------------ #
# find_setup：完整劇本（下跌 → sweep → MSS ↑ → 回踩區域）
# ------------------------------------------------------------------ #
def _uptrend_htf(n=25):
    """HTF 一路走高 → 結構 UP。"""
    ks = []
    px = 100.0
    for i in range(n):
        hi = px + (1.2 if i % 4 == 2 else 0.4)   # 每 4 根做一個 swing high
        lo = px - (1.0 if i % 4 == 0 else 0.3)
        ks.append(bar(px, hi, lo, px + 0.5, i))
        px += 0.5
    return ks


def _setup_trigger():
    """trigger 時框劇本（swing 參數 left=right=1 方便手工构造）：
    高點 → 下跌 BOS↓ → 掃低點收針 → 強拉突破 lower-high（MSS↑，留 FVG）
    → 目前價格停在高處，等回踩 Discount 的 FVG。
    前面墊 20 根等高橫盤（嚴格分形不成立 → 零 swing），滿足最少根數門檻。"""
    pad = [bar(100.0, 100.2, 99.9, 100.0, i) for i in range(20)]
    return pad + [
        bar(100.0, 101.0, 99.5, 100.5, 0),
        bar(100.5, 103.0, 100.2, 101.2, 1),   # swing high 103 @1（conf 2）
        bar(101.2, 101.6, 101.0, 101.1, 2),
        bar(100.5, 100.6, 99.8, 100.0, 3),    # swing low 99.8 @3（conf 4）
        bar(100.0, 101.0, 99.9, 100.6, 4),    # lower-high 101.0 @4（conf 5，成 protective）
        bar(100.6, 100.7, 99.0, 99.2, 5),     # 收盤破 99.8 → BOS↓（protective_high=101.0）
        bar(99.2, 99.5, 98.5, 98.8, 6),
        bar(98.8, 99.3, 98.4, 99.0, 7),       # swing low 98.4 @7（conf 8）
        bar(99.0, 99.2, 98.6, 98.9, 8),
        bar(98.9, 99.0, 97.9, 98.6, 9),       # 收針：low 97.9 掃破 98.4 池、收回上方
        bar(98.6, 99.6, 98.5, 99.5, 10),      # 反攻
        bar(99.5, 101.4, 99.4, 101.3, 11),    # 收盤破 101.0 → MSS↑；FVG c1=bar9(h99.0)
        bar(101.3, 102.4, 100.9, 102.2, 12),  # FVG@11 = [99.0, 99.4]（c3 low 99.4）
        bar(102.2, 102.5, 101.8, 102.0, 13),  # swing high 102.5 @13
        bar(102.0, 102.1, 101.5, 101.8, 14),  # 回落一點，讓 swing high 確認
    ]


def test_find_setup_produces_valid_long_plan():
    smc_cfg = {"swing": {"left": 1, "right": 1},
               "fvg": {"min_size_pct": 0.0001}}
    cfg = {"lookback_sweep_bars": 60, "min_rr_tp1": 0.5,   # 測試放寬 RR 好驗證結構
           "killzone_filter": False, "expiry_bars": 24}
    s = find_setup(_uptrend_htf(), _setup_trigger(), cfg, smc_cfg)
    assert s is not None, "完整劇本應該產出 setup"
    # 順序合理：SL < 限價 < TP1，且限價在 Discount（range 中點以下）
    assert s.stop_loss < s.limit_price < s.take_profits[0].price
    mid = (s.range_high + 97.9) / 2
    assert s.limit_price <= mid, "進場區必須在 Discount 半區"
    # SL 錨定 sweep 收針之下
    assert s.stop_loss < 97.9 * 1.0001
    # 比例總和 = 1
    assert abs(sum(t.fraction for t in s.take_profits) - 1.0) < 1e-9
    assert s.tags["zone_kind"] in ("FVG", "BPR", "IFVG", "OB", "PB")


def test_find_setup_rejects_when_htf_down():
    """HTF 空頭 → 不做多（步驟 1 過濾）。"""
    down = [bar(100 - i, 100.5 - i, 99 - i - (1.2 if i % 4 == 2 else 0),
                99.5 - i, i) for i in range(25)]
    smc_cfg = {"swing": {"left": 1, "right": 1}, "fvg": {"min_size_pct": 0.0001}}
    cfg = {"min_rr_tp1": 0.5, "killzone_filter": False}
    assert find_setup(down, _setup_trigger(), cfg, smc_cfg) is None


def test_find_setup_respects_killzone_filter():
    smc_cfg = {"swing": {"left": 1, "right": 1}, "fvg": {"min_size_pct": 0.0001}}
    cfg = {"min_rr_tp1": 0.5, "killzone_filter": True}
    assert find_setup(_uptrend_htf(), _setup_trigger(), cfg, smc_cfg,
                      killzone=None) is None, "時段外不掛新單"


def test_min_rr_tp1_filters_bad_setups():
    smc_cfg = {"swing": {"left": 1, "right": 1}, "fvg": {"min_size_pct": 0.0001}}
    cfg = {"min_rr_tp1": 50.0, "killzone_filter": False}   # 不可能達到的 RR
    assert find_setup(_uptrend_htf(), _setup_trigger(), cfg, smc_cfg) is None


# ------------------------------------------------------------------ #
# 回測煙霧測試：合成資料上跑通、統計有限、無例外
# ------------------------------------------------------------------ #
def _mtf_walk(n_entry=600, seed=11):
    """同一條 5M 走勢聚合出 15M 與 4H（三個時框完全一致、含時間戳）。"""
    ks, px, s = [], 20000.0, seed
    def rand():
        nonlocal s
        s = (s * 1103515245 + 12345) % (2 ** 31)
        return s / 2 ** 31
    for i in range(n_entry):
        o = px
        cl = px + (rand() - 0.5) * 60
        hi = max(o, cl) + 2 + rand() * 25
        lo = min(o, cl) - 2 - rand() * 25
        ks.append(bar(o, hi, lo, cl, i * 300_000))
        px = cl
    def agg(src, k_per):
        out = []
        for j in range(0, len(src) - len(src) % k_per, k_per):
            grp = src[j:j + k_per]
            out.append(bar(grp[0]["open"], max(g["high"] for g in grp),
                           min(g["low"] for g in grp), grp[-1]["close"],
                           grp[0]["time"]))
        return out
    return ks, agg(ks, 3), agg(ks, 48)


def test_backtest_e2e_fill_and_take_profit():
    """端到端：劇本延伸出回踩觸價 → 拉升打 TP，驗證成交與 R 記帳。
    entry 與 trigger 用同一序列（15M），HTF 給已收盤的上升 4H。"""
    seq = _setup_trigger()
    base_t = len(seq)
    seq = [dict(k, time=k["time"] * 900_000) for k in seq]
    ext = [
        bar(101.8, 101.9, 99.15, 99.6, base_t * 900_000),        # 回踩觸價 99.2
        bar(99.6, 103.2, 99.5, 102.8, (base_t + 1) * 900_000),   # 拉升掃 TP（103 池）
    ]
    entry = seq + ext
    htf = [dict(k, time=(i - 30) * 14_400_000)
           for i, k in enumerate(_uptrend_htf(25))]   # 全部在 entry 之前收盤
    rep = backtest_ict2022(
        entry, entry, htf,
        ict_cfg={"entry_interval": "15M", "trigger_interval": "15M",
                 "htf_interval": "4H", "min_rr_tp1": 0.5,
                 "killzone_filter": False, "expiry_bars": 24},
        smc_cfg={"swing": {"left": 1, "right": 1},
                 "fvg": {"min_size_pct": 0.0001}})
    assert rep.setups_placed >= 1, "應至少掛過一次單"
    closed = [t for t in rep.trades if t.closed]
    assert len(closed) == 1, f"應有一筆成交並出場，實際 {len(closed)}"
    t = closed[0]
    assert t.exit_reason == "tp_all", f"應以 TP 全出，實際 {t.exit_reason}"
    # 優先權 BPR > FVG：下跌段的看跌 FVG 與反攻的看漲 FVG 疊出 BPR（CE=99.75），
    # 策略應選 BPR 而非單獨的 FVG（CE=99.2）
    assert t.tags["zone_kind"] == "BPR", f"應選 BPR，實際 {t.tags['zone_kind']}"
    assert abs(t.entry - 99.75) < 1e-6, "應以 BPR 的 CE 成交"
    assert 1.2 < t.r < 2.0, f"R 應約 1.5（102.5/103 分批出、扣手續費），實際 {t.r:.2f}"


def test_backtest_runs_and_reports():
    entry, trig, htf = _mtf_walk()
    rep = backtest_ict2022(
        entry, trig, htf,
        ict_cfg={"min_rr_tp1": 0.3, "killzone_filter": False,
                 "lookback_sweep_bars": 80},
        smc_cfg={"swing": {"left": 2, "right": 2},
                 "fvg": {"min_size_pct": 0.0001}})
    assert rep.bars == len(entry)
    txt = rep.summary()
    assert txt
    for t in rep.trades:
        if t.closed:
            assert t.r == t.r and abs(t.r) < 100   # 有限且量級合理
            assert t.stop_loss < t.entry
    print(f"    （煙霧：掛單 {rep.setups_placed}、成交 {len(rep.trades)}）")


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
