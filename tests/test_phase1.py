"""Phase 1 風控升級測試：以止損決定倉位、分批停利、保本、每日上限、消息封鎖。

執行：python tests/test_phase1.py  或  python -m pytest tests/ -v
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.broker import PaperBroker  # noqa: E402
from pionexbot.executor import Executor  # noqa: E402
from pionexbot.models import Action, Signal, TakeProfit  # noqa: E402
from pionexbot.notifier import Notifier  # noqa: E402
from pionexbot.risk import RiskManager  # noqa: E402
from pionexbot.sources.webhook import parse_plan_fields  # noqa: E402
from pionexbot.store import Store  # noqa: E402

SYM = "ETH_USDT"


def _setup(price: float, risk_over: dict | None = None):
    """組一套 paper 環境：權益 10,000、每筆風險 5% = 500（規格書驗收數字）。"""
    risk_cfg = {
        "cooldown_seconds": 0,
        "max_quote_per_trade": 1_000_000,
        "max_position_base": 1_000,
        "daily_loss_limit_quote": 1_000_000,
        "risk_per_trade_pct": 0.05,
        "paper_equity": 10_000,
        "max_trades_per_day": 0,      # 預設不限，各測試自行覆寫
        "breakeven_after_tp": 1,
        "breakeven_offset_pct": 0.001,
    }
    risk_cfg.update(risk_over or {})
    store = Store(":memory:")
    broker = PaperBroker(client=None, fee_rate=0.0)  # 零手續費，數字才對得上講義
    broker._last_price_cache[SYM] = price
    execu = Executor(broker, RiskManager(risk_cfg), store, Notifier(),
                     risk_cfg=risk_cfg)
    return store, broker, execu


# ------------------------------------------------------------------ #
# 倉位計算（規格書 §2.2 驗收用例：本金 10,000、每筆風險 500）
# ------------------------------------------------------------------ #
def test_sizing_entry_3750_sl_3700_gives_10_units():
    store, broker, execu = _setup(3750.0)
    execu.handle(Signal(Action.BUY, SYM, stop_loss=3700.0))
    pos = store.load_position()
    assert abs(pos.base - 10.0) < 1e-9, f"倉位應為 10 顆，實際 {pos.base}"
    # 觸 SL 虧損 = 10 × (3750-3700) = 500
    assert abs(pos.base * (3750 - 3700) - 500.0) < 1e-6
    # 觸 TP 3900 獲利 = 10 × 150 = 1500
    assert abs(pos.base * (3900 - 3750) - 1500.0) < 1e-6


def test_sizing_entry_3720_sl_3700_gives_25_units():
    store, broker, execu = _setup(3720.0)
    execu.handle(Signal(Action.BUY, SYM, stop_loss=3700.0))
    pos = store.load_position()
    assert abs(pos.base - 25.0) < 1e-9, f"倉位應為 25 顆，實際 {pos.base}"
    assert abs(pos.base * (3720 - 3700) - 500.0) < 1e-6      # 觸 SL 虧 500
    assert abs(pos.base * (3900 - 3720) - 4500.0) < 1e-6     # 觸 TP 賺 4500


def test_signal_risk_quote_overrides_config():
    store, broker, execu = _setup(3750.0)
    execu.handle(Signal(Action.BUY, SYM, stop_loss=3700.0, risk_quote=100.0))
    assert abs(store.load_position().base - 2.0) < 1e-9      # 100/50 = 2 顆


# ------------------------------------------------------------------ #
# 非法輸入：拒單且不 crash
# ------------------------------------------------------------------ #
def test_sl_above_entry_rejected():
    store, broker, execu = _setup(3750.0)
    r = execu.handle(Signal(Action.BUY, SYM, stop_loss=3800.0))
    assert r is None and store.load_position().base == 0


def test_tp_fractions_over_one_rejected():
    store, broker, execu = _setup(3750.0)
    r = execu.handle(Signal(Action.BUY, SYM, stop_loss=3700.0,
                            take_profits=[TakeProfit(3800, 0.6),
                                          TakeProfit(3900, 0.6)]))
    assert r is None and store.load_position().base == 0


def test_webhook_plan_parsing_and_validation():
    sl, tps, rq = parse_plan_fields({
        "sl": 64000, "risk_quote": 50,
        "tps": [{"price": 68000, "fraction": 0.5},
                {"price": 70000, "fraction": 0.5}]})
    assert sl == 64000 and rq == 50 and len(tps) == 2
    for bad in (
        {"tps": [{"price": 68000, "fraction": 0.7},
                 {"price": 70000, "fraction": 0.7}]},   # 比例總和 > 1
        {"sl": 64000, "tps": [{"price": 60000, "fraction": 0.5}]},  # TP 低於 SL
        {"risk_quote": -5},                              # 風險金額為負
        {"tps": []},                                     # 空陣列
    ):
        try:
            parse_plan_fields(bad)
        except ValueError:
            continue
        raise AssertionError(f"應拒絕 {bad!r}")


# ------------------------------------------------------------------ #
# e2e（paper）：進場 → TP1 部分平倉 → 保本 → SL 出場
# ------------------------------------------------------------------ #
def test_plan_lifecycle_tp1_breakeven_then_sl():
    store, broker, execu = _setup(3750.0)
    execu.handle(Signal(Action.BUY, SYM, stop_loss=3700.0,
                        take_profits=[TakeProfit(3800, 0.5),
                                      TakeProfit(3900, 0.5)]))
    assert abs(store.load_position().base - 10.0) < 1e-9
    assert store.load_plan() is not None

    # TP1 觸價 → 賣一半（5 顆），並啟動保本
    broker._last_price_cache[SYM] = 3800.0
    execu.check_plan(3800.0)
    pos = store.load_position()
    assert abs(pos.base - 5.0) < 1e-9, f"TP1 後應剩 5 顆，實際 {pos.base}"
    plan = store.load_plan()
    assert plan["tps"][0]["filled"] and not plan["tps"][1]["filled"]
    expected_be = 3750.0 * 1.001
    assert abs(plan["stop_loss"] - expected_be) < 1e-6, "SL 應移到保本"

    # 收盤跌破保本 SL → 平掉剩餘，計畫清除
    broker._last_price_cache[SYM] = 3740.0
    execu.check_plan(3740.0, candle_close=3740.0)
    assert store.load_position().base == 0
    assert store.load_plan() is None

    # SQLite 記錄：1 買 + 2 賣；總已實現 = (3800-3750)*5 + (3740-3750)*5 = 200
    trades = store.recent_trades(10)
    sides = [t["side"] for t in trades]
    assert sides.count("BUY") == 1 and sides.count("SELL") == 2
    total_pnl = sum(t["realized_pnl"] for t in trades)
    assert abs(total_pnl - 200.0) < 1e-6, f"總損益應為 200，實際 {total_pnl}"


def test_same_candle_tp_and_sl_resolves_to_sl():
    """同一根 K 同時滿足 TP 觸價與 SL 收盤觸發 → 以 SL 計（worst）。"""
    store, broker, execu = _setup(3750.0)
    execu.handle(Signal(Action.BUY, SYM, stop_loss=3700.0,
                        take_profits=[TakeProfit(3800, 0.5)]))
    # 一根大波動 K：高點掃到 3800（TP），收盤 3690（跌破 SL）
    broker._last_price_cache[SYM] = 3690.0
    execu.check_plan(3800.0, candle_close=3690.0)
    assert store.load_position().base == 0
    trades = store.recent_trades(10)
    sells = [t for t in trades if t["side"] == "SELL"]
    assert len(sells) == 1, "應只有一筆 SL 全平，不該有 TP 部分平倉"
    assert abs(sells[0]["base"] - 10.0) < 1e-9


# ------------------------------------------------------------------ #
# 每日開單上限、消息封鎖
# ------------------------------------------------------------------ #
def test_max_trades_per_day_blocks_new_entries():
    store, broker, execu = _setup(3750.0, {"max_trades_per_day": 1})
    execu.handle(Signal(Action.BUY, SYM, quote_amount=100))
    assert store.load_position().base > 0
    before = store.load_position().base
    r = execu.handle(Signal(Action.BUY, SYM, quote_amount=100))
    assert r is None and store.load_position().base == before, "第 2 單應被每日上限擋下"
    # 出場不受影響
    r2 = execu.handle(Signal(Action.CLOSE, SYM))
    assert r2 is not None and r2.ok and store.load_position().base == 0


def test_news_window_blocks_new_entries():
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(f'- name: "US CPI"\n  time_utc: "{now_iso}"\n'
                 "  block_before_min: 30\n  block_after_min: 30\n")
        cal_path = fh.name
    try:
        store, broker, execu = _setup(3750.0, {"news_calendar_path": cal_path})
        r = execu.handle(Signal(Action.BUY, SYM, quote_amount=100))
        assert r is None and store.load_position().base == 0, "消息視窗內應拒絕新進場"
    finally:
        os.unlink(cal_path)


def test_signal_without_sl_keeps_legacy_behavior():
    """未帶 SL 的訊號走原本 quote_per_trade 流程（向下相容）。"""
    store, broker, execu = _setup(3750.0)
    execu.handle(Signal(Action.BUY, SYM, quote_amount=75.0))
    pos = store.load_position()
    assert abs(pos.base - 75.0 / 3750.0) < 1e-9
    assert store.load_plan() is None    # 沒有 SL 就不建計畫


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
