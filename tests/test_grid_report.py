"""網格績效儀表（路 1）＋壓測 wiring（路 2）測試——離線可跑。

核心情境：資料庫的 realized_pnl 只記收割、不記 grid:close 倒貨，
儀表必須用現金流重建把隱藏的重開虧損挖出來。

執行：python tests/test_grid_report.py
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.report import build_grid_report, format_grid_report  # noqa: E402

DAY = 86400.0


def _row(ts, side, base, quote, price, source, pnl=0.0, sim=0):
    return {"ts": ts, "side": side, "base": base, "quote": quote,
            "price": price, "source": source, "realized_pnl": pnl,
            "simulated": sim}


def _scenario():
    """週期1：買 100+90、收割賣 100（記+10）、跌破倒貨賣 70（沒記虧損）→ 真實 −20。
    週期2（進行中）：買 70、收割賣 75（記+5）、再買 68，存貨 10 顆。"""
    return [
        _row(1000, "BUY", 10, 100.0, 10.0, "grid"),
        _row(2000, "BUY", 10, 90.0, 9.0, "grid"),
        _row(3000, "SELL", 10, 100.0, 10.0, "grid", pnl=10.0),   # 收割
        _row(5000, "SELL", 10, 70.0, 7.0, "grid:close"),          # 倒貨（沒記虧）
        _row(6000, "BUY", 10, 70.0, 7.0, "grid"),
        _row(7000, "SELL", 10, 75.0, 7.5, "grid", pnl=5.0),       # 收割
        _row(8000, "BUY", 10, 68.0, 6.8, "grid"),
    ]


def test_true_pnl_exposes_hidden_reset_loss():
    d = build_grid_report(_scenario(), grid_capital=96.0,
                          current_price=7.0, now_ts=1000 + 10 * DAY)
    # 現金流：賣 245 − 買 328 + 存貨 10×7 = −13
    assert abs(d["true_pnl"] - (-13.0)) < 1e-9, d["true_pnl"]
    # 帳面 realized 只有收割的 +15 —— 差距就是被藏起來的重開虧損
    assert abs(d["recorded_realized"] - 15.0) < 1e-9
    assert d["true_pnl"] < d["recorded_realized"], "儀表必須挖出隱藏虧損"


def test_cycle_split_and_pnls():
    d = build_grid_report(_scenario(), grid_capital=96.0,
                          current_price=7.0, now_ts=1000 + 10 * DAY)
    assert d["cycles_total"] == 2 and d["cycles_closed"] == 1
    # 週期1（已關）：170 − 190 = −20；週期2（進行中）：75 − 138 + 70 = +7
    assert abs(d["closed_cycle_pnls"][0] - (-20.0)) < 1e-9
    assert abs(d["open_cycle_pnl"] - 7.0) < 1e-9
    assert abs(sum(d["closed_cycle_pnls"]) + d["open_cycle_pnl"]
               - d["true_pnl"]) < 1e-9, "週期損益總和必須等於真實總損益"


def test_harvest_stats_and_annualization():
    d = build_grid_report(_scenario(), grid_capital=96.0,
                          current_price=7.0, now_ts=1000 + 10 * DAY)
    assert d["harvests"] == 2
    assert abs(d["harvest_avg"] - 7.5) < 1e-9          # (10+5)/2
    assert abs(d["span_days"] - 10.0) < 1e-3
    # 年化 = −13/96/10×365 ≈ −49.4%（負得很誠實）
    assert d["annualized"] < 0
    assert abs(d["annualized"] - (-13.0 / 96 / 10 * 365)) < 1e-6
    # 同期買入持有：10 → 7 = −30%
    assert abs(d["buy_hold_return"] - (-0.30)) < 1e-9


def test_inventory_and_report_text():
    d = build_grid_report(_scenario(), grid_capital=96.0,
                          current_price=7.0, now_ts=1000 + 10 * DAY)
    assert abs(d["inventory_base"] - 10.0) < 1e-9
    txt = format_grid_report(d)
    assert "真實總損益 = -13.00" in txt
    assert "USDT" in txt and "年化" in txt


def test_empty_rows():
    d = build_grid_report([], grid_capital=96.0, current_price=7.0, now_ts=0)
    assert d["empty"] and "沒有網格" in format_grid_report(d)


def test_store_trades_by_source(tmp_path=None):
    import tempfile

    from pionexbot.store import Store
    with tempfile.TemporaryDirectory() as td:
        st = Store(os.path.join(td, "t.db"))
        st.record_trade(symbol="BTC_USDT", side="BUY", base=1, quote=10,
                        price=10, simulated=False, source="grid")
        st.record_trade(symbol="BTC_USDT", side="SELL", base=1, quote=11,
                        price=11, simulated=False, source="grid:close")
        st.record_trade(symbol="BTC_USDT", side="BUY", base=1, quote=10,
                        price=10, simulated=False, source="strategy:rsi")
        rows = st.trades_by_source("grid")
        assert len(rows) == 2, "前綴過濾：strategy 不得混入"
        assert rows[0]["side"] == "BUY" and rows[1]["source"] == "grid:close"
        st.close()


def test_close_grid_records_realized_pnl():
    """病根修正：grid:close 倒貨必須把「賣得 − 成本」記進 realized_pnl，
    「歷次累計已實現」才不會只算收割贏、藏掉重開虧。"""
    import tempfile

    from pionexbot.sources.grid_runner import GridRunner
    from pionexbot.store import Store

    class FakeRes:
        ok, simulated, error = True, False, ""
        filled_base, filled_quote, avg_price = 20.0, 140.0, 7.0

    class FakeBroker:
        def market_sell(self, symbol, qty):
            return FakeRes()

    class FakeNotifier:
        tg_enabled = discord_bot_enabled = False

        def send(self, *a, **kw):
            pass

    with tempfile.TemporaryDirectory() as td:
        store = Store(os.path.join(td, "t.db"))
        cfg = SimpleNamespace(symbol="BTC_USDT",
                              raw={"grid": {"grids": 10}}, notify={})
        gr = GridRunner(cfg, None, FakeBroker(), store, FakeNotifier())
        # 網格 [100, 110]×10 格（step 1）：持有格 0（買在 100）與格 5（買在 105）
        state = {"active": True, "lower": 100.0, "upper": 110.0, "grids": 10,
                 "held": {"0": 10.0, "5": 10.0}, "realized": 3.0}
        gr._close_grid(state, 7.0, "跌破下緣")
        row = store.trades_by_source("grid:close")[0]
        # 成本 = 10×100 + 10×105 = 2050；賣得 140 → realized −1910
        assert abs(row["realized_pnl"] - (140.0 - 2050.0)) < 1e-9, \
            f"倒貨損益應入帳，實際 {row['realized_pnl']}"
        assert not state["active"] and state["held"] == {}
        store.close()


def test_grid_stress_wiring_offline():
    """grid-stress 端到端 wiring：假幣安客戶端 + fixture K 線，離線跑通。"""
    import json

    import main as m

    ks = json.load(open(os.path.join(os.path.dirname(__file__),
                                     "fixtures", "btc_15m_v11.json")))

    class FakeBinance:
        def __init__(self, *a, **kw):
            pass

        def get_klines_range(self, symbol, interval, s, e):
            return ks

    orig = m.__dict__.get("BinanceClient")
    import pionexbot.binance_client as bc
    orig_cls = bc.BinanceClient
    bc.BinanceClient = FakeBinance
    try:
        cfg = SimpleNamespace(
            symbol="BTC_USDT",
            raw={"grid": {"grids": 8, "quote_per_grid": 12,
                          "range_mode": "atr", "atr_mult": 6,
                          "regime_filter": True, "adx_max": 30},
                 "backtest": {}})
        bot = SimpleNamespace(cfg=cfg)
        args = SimpleNamespace(symbol=None, interval="15M",
                               start="2026-07-01", end="2026-07-14",
                               base_url=None, sweep=True)
        rc = m.cmd_grid_stress(bot, args)
        assert rc == 0
    finally:
        bc.BinanceClient = orig_cls
        if orig is not None:
            m.__dict__["BinanceClient"] = orig


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
