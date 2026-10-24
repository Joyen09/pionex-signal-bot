"""跌破不認賠全賣（hold_on_breach）的行為測試。

這個開關改的是實盤真錢的行為，所以要鎖死三件事：
1. 開著時，跌破下緣**不會**產生 grid:close 的倒貨成交，存貨要原封不動。
2. 價格回來時，網格要還活著、能照常收割——不能變成「抱著但再也不動」。
3. max_loss_quote 不受這個開關影響：那是另外明確設的停損，仍然要賣。

執行：python tests/test_hold_on_breach.py  或  python -m pytest tests/ -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.broker import PaperBroker  # noqa: E402
from pionexbot.config import Config  # noqa: E402
from pionexbot.notifier import Notifier  # noqa: E402
from pionexbot.sources.grid_runner import GridRunner  # noqa: E402
from pionexbot.store import Store  # noqa: E402


def _runner(hold: bool, max_loss: float = 0):
    raw = {"trading": {"symbol": "BTC_USDT"},
           "grid": {"auto_range": True, "range_pct": 0.10, "grids": 8,
                    "quote_per_grid": 12, "breakout_buffer": 0.02,
                    "reset_on_breakout": True, "hold_on_breach": hold,
                    "max_loss_quote": max_loss}}
    store = Store(":memory:")
    broker = PaperBroker(client=None)
    r = GridRunner(Config(mode="paper", raw=raw), None, broker, store, Notifier())
    return r, store, broker


def _setp(broker, p):
    broker._last_price_cache["BTC_USDT"] = p


def _dumps(store):
    return [dict(t) for t in store.all_trades() if t["source"] == "grid:close"]


# ------------------------------------------------------------------ #
def test_default_is_off_so_live_behaviour_is_unchanged():
    """沒設這個鍵的設定檔，行為必須和以前完全一樣（會倒貨）。"""
    raw = {"trading": {"symbol": "BTC_USDT"},
           "grid": {"auto_range": True, "range_pct": 0.10, "grids": 8,
                    "quote_per_grid": 12}}
    r = GridRunner(Config(mode="paper", raw=raw), None, PaperBroker(client=None),
                   Store(":memory:"), Notifier())
    assert r.hold_on_breach is False


def test_breach_dumps_when_switch_is_off():
    """對照組：開關關著，跌破就該倒貨（這是現行實盤行為）。"""
    r, store, b = _runner(hold=False)
    _setp(b, 60000); r.run_once()
    _setp(b, 57000); r.run_once()          # 買進幾格
    assert store.load_grid_state()["held"], "應先有存貨才測得到倒貨"
    _setp(b, 40000); r.run_once()          # 崩跌穿底
    assert _dumps(store), "開關關著時，跌破必須倒貨"


def test_breach_does_not_sell_when_switch_is_on():
    r, store, b = _runner(hold=True)
    _setp(b, 60000); r.run_once()
    _setp(b, 57000); r.run_once()
    held_before = dict(store.load_grid_state()["held"])
    assert held_before, "應先有存貨"
    _setp(b, 40000); r.run_once()          # 崩跌穿底
    st = store.load_grid_state()
    assert not _dumps(store), "開著時不得有 grid:close 倒貨成交"
    assert st["active"], "網格必須還活著，不能被關掉"
    for k, v in held_before.items():
        assert st["held"].get(k) == v, f"第 {k} 格的存貨被動到了"


def test_grid_keeps_harvesting_after_price_recovers():
    """抱著等的重點是『等得到』——價格回來要能照常收割。"""
    r, store, b = _runner(hold=True)
    _setp(b, 60000); r.run_once()
    _setp(b, 57000); r.run_once()
    lower = store.load_grid_state()["lower"]
    _setp(b, 40000); r.run_once()                       # 跌破，抱著
    sells_before = sum(1 for t in store.all_trades() if t["side"] == "SELL")
    for p in (lower * 1.01, 62000, 64000, 66000):       # 價格回來
        _setp(b, p); r.run_once()
    sells_after = sum(1 for t in store.all_trades() if t["side"] == "SELL")
    assert sells_after > sells_before, "價格回來後必須能收割，否則等於卡死"
    assert not _dumps(store), "整段都不該出現倒貨"


def test_grid_is_not_repositioned_while_holding_through():
    """不賣就不該重開網格——重開會把套牢的格子留在舊區間、對不回來。"""
    r, store, b = _runner(hold=True)
    _setp(b, 60000); r.run_once()
    _setp(b, 57000); r.run_once()
    lower_before = store.load_grid_state()["lower"]
    _setp(b, 40000); r.run_once()
    assert store.load_grid_state()["lower"] == lower_before, "區間被重新定位了"


def test_max_loss_still_sells_even_when_switch_is_on():
    """max_loss_quote 是使用者另外明確設的停損，不該被這個開關默默關掉。"""
    r, store, b = _runner(hold=True, max_loss=0.5)
    _setp(b, 60000); r.run_once()
    _setp(b, 57000); r.run_once()
    assert store.load_grid_state()["held"], "應先有存貨"
    _setp(b, 40000); r.run_once()          # 浮虧遠超過 0.5
    assert _dumps(store), "設了 max_loss_quote 就必須照賣"


def test_flat_and_out_of_range_still_repositions():
    """空手出界時搬家零成本，這個行為不受開關影響。"""
    r, store, b = _runner(hold=True)
    _setp(b, 60000); r.run_once()
    st = store.load_grid_state()
    assert not st["held"], "這個情境要空手"
    lower_before = st["lower"]
    _setp(b, 40000); r.run_once()
    assert store.load_grid_state()["lower"] < lower_before, "空手出界應重新定位"
    assert not _dumps(store), "空手時沒東西可賣，不該有倒貨成交"


def test_status_text_shows_the_hold_setting():
    r, store, b = _runner(hold=True)
    _setp(b, 60000); r.run_once()
    _setp(b, 57000); r.run_once()
    assert "抱著等" in r.status_text(), "狀態要看得出這個設定開著"
    _setp(b, 40000); r.run_once()
    assert "已跌破" in r.status_text(), "跌破後狀態要講明現在正在抱著"


def test_breach_notification_is_sent_once_not_every_poll():
    """輪詢 20 秒一次，每輪都通知會洗版。"""
    sent = []

    class Spy(Notifier):
        def send(self, msg, level="info", **kw):
            sent.append(msg)

    raw = {"trading": {"symbol": "BTC_USDT"},
           "grid": {"auto_range": True, "range_pct": 0.10, "grids": 8,
                    "quote_per_grid": 12, "breakout_buffer": 0.02,
                    "reset_on_breakout": True, "hold_on_breach": True}}
    store, b = Store(":memory:"), PaperBroker(client=None)
    r = GridRunner(Config(mode="paper", raw=raw), None, b, store, Spy())
    _setp(b, 60000); r.run_once()
    _setp(b, 57000); r.run_once()
    sent.clear()
    for _ in range(5):                      # 連跌 5 輪
        _setp(b, 40000); r.run_once()
    assert sum(1 for m in sent if "抱著等" in m) == 1, f"通知了多次：{sent}"


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
