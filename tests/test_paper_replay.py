"""紙上成交 vs 回測模擬的比對工具測試（不需要行情資料，只測配對邏輯）。

執行：python tests/test_paper_replay.py  或  python -m pytest tests/ -v
"""
from __future__ import annotations

import os
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grid_paper_replay import load_real, match  # noqa: E402

T0 = 1_780_000_000


def _real(path, rows):
    pd.DataFrame([{"trade_id": i, "ts_epoch": ts, "symbol": "BTC_USDT",
                   "side": side, "price": px, "source": src}
                  for i, (ts, side, px, src) in enumerate(rows, 1)]
                 ).to_csv(path, index=False, encoding="utf-8-sig")
    return load_real(path)


def _ev(rows):
    return [{"ts": ts, "kind": k, "price": px, "level": 0, "detail": ""}
            for ts, k, px in rows]


def test_dump_is_separated_from_normal_sell():
    """grid:close 是跌破倒貨，不能和一般收割賣出混為一談。"""
    with tempfile.TemporaryDirectory() as d:
        r = _real(os.path.join(d, "t.csv"), [
            (T0, "BUY", 100.0, "grid"),
            (T0 + 60, "SELL", 101.0, "grid"),
            (T0 + 120, "SELL", 90.0, "grid:close"),
        ])
        assert list(r.kind) == ["BUY", "SELL", "DUMP"]


def test_non_grid_trades_are_excluded():
    with tempfile.TemporaryDirectory() as d:
        r = _real(os.path.join(d, "t.csv"), [
            (T0, "BUY", 100.0, "grid"),
            (T0 + 60, "BUY", 100.0, "ict2022"),
            (T0 + 120, "SELL", 101.0, "plan:tp"),
        ])
        assert len(r) == 1 and r.source.iloc[0] == "grid"


def test_perfect_match_is_100_percent():
    with tempfile.TemporaryDirectory() as d:
        rows = [(T0, "BUY", 100.0, "grid"), (T0 + 600, "SELL", 101.0, "grid")]
        r = _real(os.path.join(d, "t.csv"), rows)
        ev = _ev([(T0, "BUY", 100.0), (T0 + 600, "SELL", 101.0)])
        _, s = match(r, ev, tol_min=5)
        assert s["matched"] == 2 and s["real_total"] == 2 and s["sim_total"] == 2


def test_one_sim_event_cannot_match_many_real_trades():
    """一對一配對：模擬只買 1 次、實際買 3 次，不能看起來全部命中。"""
    with tempfile.TemporaryDirectory() as d:
        r = _real(os.path.join(d, "t.csv"), [
            (T0, "BUY", 100.0, "grid"),
            (T0 + 60, "BUY", 100.0, "grid"),
            (T0 + 120, "BUY", 100.0, "grid"),
        ])
        _, s = match(r, _ev([(T0, "BUY", 100.0)]), tol_min=5)
        assert s["matched"] == 1, "一個模擬事件被配走多次"
        assert s["counts"]["BUY"] == {"實際": 3, "模擬": 1}


def test_outside_tolerance_does_not_match():
    with tempfile.TemporaryDirectory() as d:
        r = _real(os.path.join(d, "t.csv"), [(T0, "BUY", 100.0, "grid")])
        assert match(r, _ev([(T0 + 4 * 60, "BUY", 100.0)]), 5)[1]["matched"] == 1
        assert match(r, _ev([(T0 + 6 * 60, "BUY", 100.0)]), 5)[1]["matched"] == 0


def test_direction_must_agree():
    """時間再近，方向不同就不算配對。"""
    with tempfile.TemporaryDirectory() as d:
        r = _real(os.path.join(d, "t.csv"), [(T0, "BUY", 100.0, "grid")])
        _, s = match(r, _ev([(T0, "SELL", 100.0)]), tol_min=5)
        assert s["matched"] == 0


def test_sim_only_events_are_reported_as_rows():
    """模擬做了、實際沒做的動作必須出現在明細裡，不能靜靜消失。"""
    with tempfile.TemporaryDirectory() as d:
        r = _real(os.path.join(d, "t.csv"), [(T0, "BUY", 100.0, "grid")])
        df, s = match(r, _ev([(T0, "BUY", 100.0),
                              (T0 + 7200, "SELL", 110.0)]), tol_min=5)
        assert s["sim_total"] == 2 and s["matched"] == 1
        assert (df["配對"] == "✗ 實際沒有對應成交").sum() == 1


def test_match_rate_uses_union_not_real_count():
    """漏掉的實際成交若不計入分母，吻合度會自己看起來很漂亮。"""
    with tempfile.TemporaryDirectory() as d:
        r = _real(os.path.join(d, "t.csv"), [(T0, "BUY", 100.0, "grid")])
        ev = _ev([(T0, "BUY", 100.0)] +
                 [(T0 + 7200 * i, "SELL", 110.0) for i in range(1, 10)])
        _, s = match(r, ev, tol_min=5)
        union = s["real_total"] + s["sim_total"] - s["matched"]
        assert s["matched"] / s["real_total"] == 1.0, "只看實際筆數會是 100%"
        assert s["matched"] / union == 0.1, "以聯集為分母才看得出差很多"


def test_export_grid_meta_includes_range_pct_and_created_time():
    """匯出必須帶上區間寬度與開網格時間，否則重播對不齊起點。"""
    from pionexbot.config import Config
    from pionexbot.export import grid_meta
    cfg = Config(mode="paper", raw={
        "trading": {"symbol": "BTC_USDT"},
        "grid": {"grids": 8, "quote_per_grid": 12, "range_mode": "fixed",
                 "range_pct": 0.15, "auto_range": True},
    })
    state = {"active": True, "lower": 60000.0, "upper": 68000.0, "grids": 8,
             "held": {}, "created_price": 64000.0, "created_ts": 1_750_000_000.0}
    m = grid_meta(cfg, state, first_trade_ts=1_750_000_500.0)
    assert m["range_pct"] == 0.15 and m["range_mode"] == "fixed"
    assert m["current_grid_created_utc"] == "2025-06-15 15:06:40"
    # 開網格時間必須早於第一筆成交——用成交時間當起點會少掉前面那段
    assert m["current_grid_created_utc"] < m["first_trade_utc"]


def test_grid_meta_tolerates_old_state_without_created_ts():
    from pionexbot.config import Config
    from pionexbot.export import grid_meta
    cfg = Config(mode="paper", raw={"trading": {"symbol": "BTC_USDT"},
                                    "grid": {"grids": 8, "quote_per_grid": 12}})
    m = grid_meta(cfg, {"active": True, "lower": 1.0, "upper": 2.0,
                        "grids": 8, "held": {}}, first_trade_ts=None)
    assert "current_grid_created_utc" not in m, "舊狀態檔不該憑空生出時間"
    assert m["range_pct"] == "", "沒設定就留空，不要填假的預設值"


def test_counts_are_separated_by_kind():
    """次數比對是主要判準，三種動作不能混在一起算。"""
    import tempfile as tf
    with tf.TemporaryDirectory() as d:
        r = _real(os.path.join(d, "t.csv"), [
            (T0, "BUY", 100.0, "grid"),
            (T0 + 60, "SELL", 101.0, "grid"),
            (T0 + 120, "SELL", 90.0, "grid:close"),
        ])
        _, s = match(r, _ev([(T0, "BUY", 100.0)]), tol_min=5)
        assert s["counts"]["BUY"] == {"實際": 1, "模擬": 1}
        assert s["counts"]["SELL"] == {"實際": 1, "模擬": 0}
        assert s["counts"]["DUMP"] == {"實際": 1, "模擬": 0}


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
