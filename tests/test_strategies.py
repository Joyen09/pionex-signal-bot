"""策略與回測引擎的離線測試（不需網路）。

執行：python tests/test_strategies.py  或  python -m pytest tests/ -v
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.backtest import Backtester  # noqa: E402
from pionexbot.models import Action  # noqa: E402
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
