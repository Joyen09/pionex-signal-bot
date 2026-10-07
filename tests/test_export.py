"""逐筆成交匯出測試：欄位完整性、手續費誠實性、持倉與網格參數。

執行：python tests/test_export.py  或  python -m pytest tests/ -v
"""
from __future__ import annotations

import csv
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.config import Config  # noqa: E402
from pionexbot.export import (TRADE_COLUMNS, grid_meta, open_positions,  # noqa: E402
                              trade_rows, write_export)
from pionexbot.store import Store  # noqa: E402


def _cfg(live=True):
    return Config(mode="live" if live else "paper", raw={
        "trading": {"symbol": "BTC_USDT", "initial_capital": 108},
        "grid": {"grids": 8, "quote_per_grid": 12, "range_mode": "atr",
                 "atr_mult": 6},
    })


def _grid_state():
    """區間 60000~68000、8 格（每格 1000），持有第 1、3 格。"""
    return {"active": True, "lower": 60000.0, "upper": 68000.0, "grids": 8,
            "held": {"1": 0.0002, "3": 0.00018}, "realized": 1.5,
            "created_price": 64000.0, "last_price": 65000.0}


def _rows():
    return [
        {"id": 1, "ts": 1_750_000_000.0, "symbol": "BTC_USDT", "side": "BUY",
         "base": 0.0002, "quote": 12.2, "price": 61000.0, "simulated": 0,
         "source": "grid", "order_id": "o1", "realized_pnl": 0.0,
         "fee": None, "fee_coin": None},
        {"id": 2, "ts": 1_750_086_400.0, "symbol": "BTC_USDT", "side": "SELL",
         "base": 0.0002, "quote": 12.4, "price": 62000.0, "simulated": 0,
         "source": "grid", "order_id": "o2", "realized_pnl": 0.2,
         "fee": 0.0062, "fee_coin": "USDT"},
    ]


# ---------------- 逐筆列 ----------------
def test_trade_rows_have_all_requested_fields():
    r = trade_rows(_rows())[0]
    for col in ("time_utc", "time_taipei", "symbol", "side", "price",
                "base_qty", "quote_amount", "fee", "fee_coin"):
        assert col in r, f"缺少欄位 {col}"
    assert r["side"] == "BUY" and r["symbol"] == "BTC_USDT"
    assert r["price"].startswith("61000")
    assert r["base_qty"].startswith("0.0002")


def test_missing_fee_is_blank_not_zero():
    """未記錄的手續費必須留空——填 0 會被誤讀成免手續費，讓分析偏樂觀。"""
    rows = trade_rows(_rows())
    assert rows[0]["fee"] == "" and rows[0]["fee_coin"] == ""
    assert rows[1]["fee"] == 0.0062 and rows[1]["fee_coin"] == "USDT"


def test_rows_sorted_and_not_paired():
    """逐筆原始資料：不配對、不聚合，買賣各自獨立一列。"""
    out = trade_rows(list(reversed(_rows())))
    assert [r["side"] for r in out] == ["BUY", "SELL"], "需依時間升冪"
    assert len(out) == 2, "兩筆成交不得被配對成一列"


def test_taipei_time_is_utc_plus_8():
    r = trade_rows(_rows())[0]
    assert r["time_utc"] == "2025-06-15 15:06:40", r["time_utc"]
    assert r["time_taipei"] == "2025-06-15 23:06:40", r["time_taipei"]
    # 同一時刻、兩種時區：分秒相同、時差 8 小時
    assert r["time_utc"][14:] == r["time_taipei"][14:]
    assert int(r["time_taipei"][11:13]) == (int(r["time_utc"][11:13]) + 8) % 24


# ---------------- 未平倉持倉 ----------------
def test_open_positions_lots_and_unrealized():
    pos = open_positions(_grid_state(), current_price=60500.0)
    assert pos["active"] and len(pos["lots"]) == 2
    # 第 1 格 = 60000 + 1×1000 = 61000；第 3 格 = 63000
    assert [lot["level_price"] for lot in pos["lots"]] == [61000.0, 63000.0]
    assert abs(pos["total_base"] - 0.00038) < 1e-12
    cost = 61000 * 0.0002 + 63000 * 0.00018
    assert abs(pos["cost_value"] - cost) < 1e-9
    assert abs(pos["avg_cost"] - cost / 0.00038) < 1e-6
    # 現價低於均價 → 浮虧（網格的風險就藏在這）
    assert pos["unrealized"] < 0, "現價 60500 低於均價，應為浮虧"


def test_open_positions_empty_when_no_grid():
    assert open_positions(None, 60000.0)["total_base"] == 0.0
    flat = {**_grid_state(), "held": {}}
    assert open_positions(flat, 60000.0)["total_base"] == 0.0


def test_grid_meta_has_params_and_start_time():
    m = grid_meta(_cfg(), _grid_state(), first_trade_ts=1_750_000_000.0)
    assert m["grids"] == 8 and m["quote_per_grid"] == 12
    assert m["allocated_capital"] == 96
    assert m["current_range_lower"].startswith("60000")
    assert m["current_range_upper"].startswith("68000")
    assert "first_trade_utc" in m and "first_trade_taipei" in m


# ---------------- 整體寫檔 ----------------
def test_write_export_creates_all_files_and_warns_about_fees():
    with tempfile.TemporaryDirectory() as d:
        info = write_export(d, rows=_rows(), cfg=_cfg(),
                            grid_state=_grid_state(), current_price=60500.0)
        names = set(os.listdir(d))
        assert {"trades.csv", "grid_meta.csv", "open_positions.csv",
                "README.txt"} <= names, names
        with open(os.path.join(d, "trades.csv"), encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 2
        assert list(rows[0].keys()) == TRADE_COLUMNS
        assert rows[0]["fee"] == "", "未記錄的手續費在 CSV 也要是空字串"
        readme = open(os.path.join(d, "README.txt"), encoding="utf-8").read()
        assert "手續費" in readme and "派網 App" in readme, "須指路權威來源"
        assert "未實現" in readme, "須提醒浮虧風險"
        assert info["positions"]["unrealized"] < 0


def test_export_reads_from_real_store_with_migration():
    """舊資料庫（無 fee 欄）→ 開啟即遷移 → 匯出不報錯、舊列 fee 為空。"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "bot.db")
        c = sqlite3.connect(p)
        c.executescript(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts REAL NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,"
            " base REAL NOT NULL, quote REAL NOT NULL, price REAL NOT NULL,"
            " simulated INTEGER NOT NULL, source TEXT, order_id TEXT,"
            " realized_pnl REAL DEFAULT 0);"
            "CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
        c.execute("INSERT INTO trades(ts,symbol,side,base,quote,price,simulated,"
                  "source,order_id,realized_pnl) VALUES"
                  "(1750000000,'BTC_USDT','BUY',0.0002,12.2,61000,0,'grid','o1',0)")
        c.commit()
        c.close()

        store = Store(p)                      # 觸發遷移
        rows = [dict(r) for r in store.all_trades(symbol="BTC_USDT")]
        assert len(rows) == 1 and rows[0]["fee"] is None, "舊列不得被填成 0"
        out = os.path.join(d, "export")
        info = write_export(out, rows=rows, cfg=_cfg(), grid_state=None,
                            current_price=60000.0)
        assert info["trades"] == 1


def test_all_trades_filters_paper_and_live():
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "t.db"))
        s.record_trade(symbol="BTC_USDT", side="BUY", base=1, quote=1, price=1,
                       simulated=False, source="grid")
        s.record_trade(symbol="BTC_USDT", side="BUY", base=1, quote=1, price=1,
                       simulated=True, source="grid")
        s.record_trade(symbol="ETH_USDT", side="BUY", base=1, quote=1, price=1,
                       simulated=False, source="grid")
        assert len(s.all_trades()) == 3
        assert len(s.all_trades(simulated=False)) == 2
        assert len(s.all_trades(symbol="BTC_USDT")) == 2
        assert len(s.all_trades(symbol="BTC_USDT", simulated=False)) == 1


def test_record_trade_stores_fee_and_coin():
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "t.db"))
        s.record_trade(symbol="BTC_USDT", side="SELL", base=0.001, quote=61,
                       price=61000, simulated=False, source="grid",
                       fee=0.0305, fee_coin="USDT")
        r = dict(s.all_trades()[0])
        assert r["fee"] == 0.0305 and r["fee_coin"] == "USDT"


def test_live_broker_extracts_fee_from_order_payload():
    from pionexbot.broker import LiveBroker
    assert LiveBroker._extract_fee({"fee": "0.0305", "feeCoin": "USDT"}) \
        == (0.0305, "USDT")
    assert LiveBroker._extract_fee({"commission": 0.5,
                                    "commissionAsset": "BNB"}) == (0.5, "BNB")
    # 沒有手續費欄位 → None（不是 0），否則分析會把成本當成 0
    assert LiveBroker._extract_fee({"orderId": "x"}) == (None, "")


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
