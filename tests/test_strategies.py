"""策略與回測引擎的離線測試（不需網路）。

執行：python tests/test_strategies.py  或  python -m pytest tests/ -v
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.backtest import Backtester  # noqa: E402
from pionexbot.models import Action, Signal  # noqa: E402
from pionexbot.strategy import available, build_strategy  # noqa: E402


def _klines(closes):
    return [{"close": c} for c in closes]


def test_registry_has_four_strategies():
    assert set(available()) == {"ma_cross", "rsi", "macd", "bollinger"}


def test_ma_cross_buys_on_golden_cross():
    s = build_strategy("ma_cross", {"fast": 3, "slow": 5})
    closes = [10, 10, 10, 10, 10, 10, 10, 11, 12, 13, 14]
    fired = [s.evaluate(_klines(closes[:i]), "X") for i in range(7, len(closes) + 1)]
    actions = [sig.action for sig in fired if sig]
    assert Action.BUY in actions


def test_ma_cross_rejects_bad_params():
    try:
        build_strategy("ma_cross", {"fast": 20, "slow": 5})
    except ValueError:
        return
    raise AssertionError("fast >= slow 應該要報錯")


def test_rsi_buys_after_deep_drop_then_recovery():
    s = build_strategy("rsi", {"period": 14, "oversold": 30, "overbought": 70})
    closes = [100.0]
    for _ in range(30):       # 連續下跌 -> RSI 進入超賣
        closes.append(closes[-1] * 0.98)
    for _ in range(10):       # 反彈 -> RSI 上穿超賣線
        closes.append(closes[-1] * 1.02)
    actions = [sig.action for i in range(16, len(closes) + 1)
               if (sig := s.evaluate(_klines(closes[:i]), "X"))]
    assert Action.BUY in actions


def test_strategy_returns_none_when_insufficient_data():
    for name in available():
        s = build_strategy(name, {})
        assert s.evaluate(_klines([100, 101, 102]), "X") is None


def test_backtest_runs_and_reports():
    s = build_strategy("ma_cross", {"fast": 5, "slow": 20})
    closes = [100 * (1 + 0.001 * math.sin(i / 7) + 0.0005) for i in range(300)]
    r = Backtester(s, start_cash=1000, quote_per_trade=200).run(_klines(closes), "X")
    assert r.bars == 300
    assert r.end_equity > 0
    # 勝率介於 0~1，報酬為有限數
    assert 0.0 <= r.win_rate <= 1.0
    assert math.isfinite(r.total_return)


def test_backtest_buy_hold_matches_price_change():
    s = build_strategy("ma_cross", {"fast": 3, "slow": 5})
    closes = [100, 100, 110, 120, 130, 140, 150]
    r = Backtester(s).run(_klines(closes), "X")
    # 買入持有以第 index 2 根為基準
    assert abs(r.buy_hold_return - (150 - 110) / 110) < 1e-9


def test_take_profit_closes_position():
    from pionexbot.config import Config
    from pionexbot.store import Store
    from pionexbot.risk import RiskManager
    from pionexbot.broker import PaperBroker
    from pionexbot.executor import Executor
    from pionexbot.notifier import Notifier
    from pionexbot.sources.strategy_runner import StrategyRunner

    raw = {
        "trading": {"symbol": "BTC_USDT", "quote_per_trade": 20},
        "risk": {"cooldown_seconds": 0, "max_quote_per_trade": 100,
                 "max_position_base": 1, "take_profit_pct": 0.06, "stop_loss_pct": 0.03},
        "strategy": {"name": "ma_cross", "params": {"fast": 3, "slow": 5}},
    }
    cfg = Config(mode="paper", raw=raw)
    store = Store(":memory:")
    broker = PaperBroker(client=None)
    broker._last_price_cache["BTC_USDT"] = 60000.0
    notifier = Notifier()
    execu = Executor(broker, RiskManager(raw["risk"]), store, notifier)
    runner = StrategyRunner(cfg, None, execu, notifier)

    execu.handle(Signal(Action.BUY, "BTC_USDT", quote_amount=20))
    assert store.load_position().base > 0
    pos = store.load_position(); pos.last_trade_ts = 0; store.save_position(pos)

    broker._last_price_cache["BTC_USDT"] = 64200.0   # +7% > 停利 6%
    runner._check_exit()
    assert store.load_position().base == 0            # 已平倉


def test_sweep_runs_grid():
    from pionexbot.backtest import sweep_stop_params
    closes = [100 * (1 + 0.002 * math.sin(i / 9)) for i in range(400)]
    kl = [{"close": c, "time": i} for i, c in enumerate(closes)]
    rows = sweep_stop_params("ma_cross", {"fast": 5, "slow": 20}, kl, "X",
                             sl_grid=[0.0, 0.03], tp_grid=[0.0, 0.06],
                             start_cash=1000, quote_per_trade=200)
    assert len(rows) == 4                       # 2x2 網格
    assert all(math.isfinite(r.result.total_return) for r in rows)
    assert all(math.isfinite(r.score) for r in rows)


def test_backtest_take_profit_caps_winner():
    # 一路上漲：有停利會比無停利「提早出場」，部位行為不同但都不該報錯
    from pionexbot.backtest import Backtester
    closes = [100, 100, 101, 103, 106, 110, 115, 121, 128, 136]
    kl = [{"close": c} for c in closes]
    s1 = build_strategy("ma_cross", {"fast": 2, "slow": 4})
    r = Backtester(s1, take_profit_pct=0.05).run(kl, "X")
    assert math.isfinite(r.total_return)


def test_trend_filter_blocks_buys_in_downtrend():
    s = build_strategy("ma_cross", {"fast": 3, "slow": 5, "trend_ma": 10})
    down = [100 - i for i in range(20)]
    fired = [s.evaluate(_klines(down[:i]), "X") for i in range(12, 21)]
    buys = [x for x in fired if x and x.action == Action.BUY]
    assert len(buys) == 0


def test_walk_forward_runs():
    from pionexbot.backtest import walk_forward, grid_search
    closes = [100 * (1 + 0.001 * math.sin(i / 40) + 0.0002) for i in range(1200)]
    kl = [{"close": c, "time": i} for i, c in enumerate(closes)]
    gs = grid_search("ma_cross", kl, "X", start_cash=1000, quote_per_trade=1000)
    assert len(gs) > 0
    wf = walk_forward("ma_cross", kl, "X", folds=3,
                      start_cash=1000, quote_per_trade=1000)
    assert len(wf) == 3
    for f in wf:
        assert math.isfinite(f.test_return)
        assert "slow" in f.params


def test_candle_time_extraction():
    from pionexbot.sources.strategy_runner import StrategyRunner
    assert StrategyRunner._candle_time({"time": 123, "close": 1}) == 123
    assert StrategyRunner._candle_time([999, 1, 2, 3, 4, 5]) == 999
    assert StrategyRunner._candle_time({"close": 1}) is None


def test_grid_profits_in_oscillation():
    from pionexbot.grid import GridBacktester
    osc = [100 + 10 * math.sin(i / 5) for i in range(600)]
    kl = [{"high": c, "low": c, "close": c} for c in osc]
    r = GridBacktester(90, 110, 20, quote_per_grid=20).run(kl, "X")
    assert r.completed_grids > 0
    assert r.realized_profit > 0          # 震盪應有實現利潤
    assert r.total_return > 0


def test_grid_bags_in_downtrend():
    from pionexbot.grid import GridBacktester
    down = [100 - (40 * i / 600) for i in range(600)]
    kl = [{"high": c, "low": c, "close": c} for c in down]
    r = GridBacktester(80, 120, 20, quote_per_grid=20).run(kl, "X")
    assert r.unrealized < 0               # 跌勢會套牢
    assert r.total_return < 0


def test_grid_runner_harvest_and_auto_reset():
    from pionexbot.config import Config
    from pionexbot.store import Store
    from pionexbot.broker import PaperBroker
    from pionexbot.notifier import Notifier
    from pionexbot.sources.grid_runner import GridRunner

    raw = {"trading": {"symbol": "BTC_USDT"},
           "grid": {"auto_range": True, "range_pct": 0.15, "grids": 10,
                    "quote_per_grid": 5, "breakout_buffer": 0.02,
                    "reset_on_breakout": True}}
    cfg = Config(mode="paper", raw=raw)
    store = Store(":memory:")
    broker = PaperBroker(client=None)
    runner = GridRunner(cfg, None, broker, store, Notifier())

    def setp(p):
        broker._last_price_cache["BTC_USDT"] = p

    setp(60000); runner.run_once()                      # 建立網格
    st = store.load_grid_state()
    assert st and st["active"]
    base_lower = st["lower"]

    for p in [56000, 54000, 58000, 62000, 64000]:       # 震盪
        setp(p); runner.run_once()
    assert store.load_grid_state()["realized"] >= 0     # 應有(或不虧)實現利潤

    setp(40000); runner.run_once()                      # 崩跌穿底 → 關閉重開
    st2 = store.load_grid_state()
    assert st2["active"] and st2["lower"] < base_lower   # 已在更低價重開新網格


def test_grid_rejects_bad_range():
    from pionexbot.grid import GridBacktester
    try:
        GridBacktester(110, 90, 20, quote_per_grid=20)
    except ValueError:
        return
    raise AssertionError("lower>=upper 應報錯")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✅ {name}")
            except AssertionError as exc:
                failed += 1
                print(f"  ❌ {name}: {exc}")
    print("\n全部通過" if not failed else f"\n{failed} 個失敗")
    sys.exit(1 if failed else 0)
