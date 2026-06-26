"""簽章與核心邏輯的離線單元測試（不需網路、不需金鑰）。

執行：
    python -m pytest tests/ -v
或不裝 pytest：
    python tests/test_signing.py
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.pionex_client import PionexClient  # noqa: E402
from pionexbot.models import Action, Signal  # noqa: E402
from pionexbot.risk import RiskManager  # noqa: E402
from pionexbot.store import Position  # noqa: E402


def test_sorted_query_is_ascii_ascending():
    qs = PionexClient._sorted_query({"timestamp": 100, "symbol": "BTC_USDT", "a": 1})
    # key 排序：a, symbol, timestamp
    assert qs == "a=1&symbol=BTC_USDT&timestamp=100"


def test_signature_matches_manual_hmac():
    client = PionexClient(api_key="k", api_secret="mysecret")
    query = {"timestamp": 1700000000000, "symbol": "BTC_USDT"}
    body = ""
    path_url, sig = client._sign("GET", "/api/v1/account/balances", query, body)

    expected_msg = "GET/api/v1/account/balances?symbol=BTC_USDT&timestamp=1700000000000"
    assert path_url == "/api/v1/account/balances?symbol=BTC_USDT&timestamp=1700000000000"
    expected = hmac.new(b"mysecret", expected_msg.encode(), hashlib.sha256).hexdigest()
    assert sig == expected


def test_signature_includes_body_for_post():
    client = PionexClient(api_key="k", api_secret="s")
    query = {"timestamp": 1700000000000}
    body = '{"symbol":"BTC_USDT","side":"BUY","type":"MARKET","amount":"20"}'
    _, sig = client._sign("POST", "/api/v1/trade/order", query, body)
    msg = "POST/api/v1/trade/order?timestamp=1700000000000" + body
    expected = hmac.new(b"s", msg.encode(), hashlib.sha256).hexdigest()
    assert sig == expected


def test_risk_caps_quote_to_max():
    rm = RiskManager({"max_quote_per_trade": 30, "max_position_base": 999,
                      "cooldown_seconds": 0, "daily_loss_limit_quote": 999})
    pos = Position()
    sig = Signal(action=Action.BUY, symbol="BTC_USDT", quote_amount=100)
    d = rm.check(sig, pos, price=50000)
    assert d.allowed
    assert d.quote_amount == 30  # 被壓到上限


def test_risk_blocks_sell_without_position():
    rm = RiskManager({"cooldown_seconds": 0, "allow_short": False})
    pos = Position(base=0.0)
    sig = Signal(action=Action.SELL, symbol="BTC_USDT")
    d = rm.check(sig, pos, price=50000)
    assert not d.allowed


def test_risk_cooldown_blocks():
    import time
    rm = RiskManager({"cooldown_seconds": 9999, "max_quote_per_trade": 99,
                      "max_position_base": 99, "daily_loss_limit_quote": 99})
    pos = Position(last_trade_ts=time.time())
    sig = Signal(action=Action.BUY, symbol="BTC_USDT", quote_amount=10)
    d = rm.check(sig, pos, price=50000)
    assert not d.allowed
    assert "冷卻" in d.reason


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
