"""回測成交模型測試（backtestfillfix.md §4）：觸價止損對稱於 TP。

核心契約：零滑價下，止損單成交在「止損價」而非收盤價 → 損失貼 −1R，
不再吃掉整根 K 的超越。保守規則（SL 先於 TP、進場根觸損、保本）全部保留。

出場邏輯已抽成純函數 apply_bar_exit，這裡直接對它下各種 K 線斷言——
不經過 find_setup 管線，隔離成交模型本身。fee_rate=0 以斷言乾淨的 R。

執行：python tests/test_backtest_fill.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.backtest_ict import (IctTrade, Position,  # noqa: E402
                                    apply_bar_exit)

ENTRY, SL = 100.0, 98.0                 # 風險距離 2.0 → 1R = 2.0
TP1, TP2 = 104.0, 106.0                 # +2R、+3R


def _pos(tps=((TP1, 0.5), (TP2, 0.5))):
    """新建一筆剛成交、風險 1R 的多單（qty 使 1R = risk_quote）。"""
    qty = 100.0 / (ENTRY - SL)          # risk_quote=100 → 1R=100 名目
    tr = IctTrade(entry=ENTRY, stop_loss=SL, qty=qty, risk_quote=100.0,
                  opened_at=0, tags={})
    return Position(trade=tr, plan_sl=SL, qty_left=qty,
                    tps_state=[{"price": p, "fraction": f, "filled": False}
                               for p, f in tps])


NOFEE = dict(fee_rate=0.0, slippage_pct=0.0)


# ------------------------------------------------------------------ #
# 1. 觸價止損 = −1R（收盤在止損價之上，收盤模型會整根漏掉）
# ------------------------------------------------------------------ #
def test_touch_sl_fills_at_stop_even_when_close_above():
    pos = _pos()
    # low 97.0 刺穿 SL 98.0，close 99.0 收在 SL 之上
    closed = apply_bar_exit(pos, 5, 99.5, 99.6, 97.0, 99.0, **NOFEE)
    assert closed and pos.trade.exit_reason == "sl"
    assert abs(pos.trade.exit_px - SL) < 1e-9, "成交在止損價、非收盤價"
    assert abs(pos.trade.r + 1.0) < 1e-9, f"應恰 −1R，實際 {pos.trade.r}"
    assert abs(pos.trade.sl_slip_r) < 1e-9, "觸價模型滑移為 0"


def test_deep_wick_still_only_minus_one_r():
    pos = _pos()
    # 急殺：low 90（遠穿 SL）、close 91 → 舊模型會記 ~−4.5R，觸價只 −1R
    apply_bar_exit(pos, 5, 97.0, 97.1, 90.0, 91.0, **NOFEE)
    assert abs(pos.trade.r + 1.0) < 1e-9, \
        f"再深的影線觸價止損也只 −1R，實際 {pos.trade.r}"


# ------------------------------------------------------------------ #
# 2. 同根同時觸 SL 與 TP → 以 SL 計（worst）
# ------------------------------------------------------------------ #
def test_same_bar_sl_and_tp_resolves_to_sl():
    pos = _pos()
    # 一根同時 low<=SL 且 high>=TP1 且 >=TP2
    closed = apply_bar_exit(pos, 5, 99.0, 107.0, 97.0, 100.0, **NOFEE)
    assert closed and pos.trade.exit_reason == "sl", "同根衝突須以 SL 計"
    assert abs(pos.trade.r + 1.0) < 1e-9


# ------------------------------------------------------------------ #
# 3. 進場當根即觸止損 = −1R（same_bar 只認 SL，不吃 TP）
# ------------------------------------------------------------------ #
def test_entry_bar_touch_sl_is_minus_one_r():
    pos = _pos()
    closed = apply_bar_exit(pos, 0, ENTRY, 105.0, 97.5, 99.0,
                            same_bar=True, **NOFEE)
    assert closed and pos.trade.exit_reason == "sl_same_bar"
    assert abs(pos.trade.r + 1.0) < 1e-9


def test_same_bar_ignores_tp():
    pos = _pos()
    # 進場根 high 觸 TP 但 low 未觸 SL → same_bar 不平倉（保守不吃 TP）
    closed = apply_bar_exit(pos, 0, ENTRY, 107.0, 99.5, 105.0,
                            same_bar=True, **NOFEE)
    assert not closed and pos.trade.exit_reason == "", "進場根不吃 TP"


# ------------------------------------------------------------------ #
# 4. 保本回撤 = 小正報酬
# ------------------------------------------------------------------ #
def test_breakeven_pullback_yields_small_positive():
    pos = _pos()
    # 第 1 根：high 觸 TP1（50% 落袋 +2R×0.5=+1R），保本 SL 上移 100.1
    apply_bar_exit(pos, 5, 100.0, 104.5, 100.0, 103.0, **NOFEE)
    assert pos.be_done and pos.plan_sl > ENTRY, "TP1 後止損應上移保本"
    # 第 2 根：回落觸保本（low 99）→ 餘 50% 於保本價出
    closed = apply_bar_exit(pos, 6, 103.0, 103.5, 99.0, 101.0, **NOFEE)
    assert closed and pos.trade.r > 0, f"TP1 落袋 + 餘倉保本 → 正，實際 {pos.trade.r}"
    assert pos.trade.r < 2.0, f"應遠小於整段 TP 的 R，實際 {pos.trade.r}"


# ------------------------------------------------------------------ #
# 5. 全 TP 成交 = tp_all（贏單路徑）
# ------------------------------------------------------------------ #
def test_all_tps_fill_closes_as_tp_all():
    pos = _pos()
    # 一根打穿兩個 TP、low 不觸 SL（也不觸保本，因保本尚未設）
    closed = apply_bar_exit(pos, 5, 100.0, 107.0, 99.5, 106.5, **NOFEE)
    assert closed and pos.trade.exit_reason == "tp_all"
    # +2R×0.5 + +3R×0.5 = +2.5R
    assert abs(pos.trade.r - 2.5) < 1e-9, f"應 +2.5R，實際 {pos.trade.r}"


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
