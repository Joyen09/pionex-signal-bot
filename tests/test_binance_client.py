"""幣安客戶端測試：正規化、陣列→dict、分頁邏輯（用假 session，離線可跑）。

沙盒連不到幣安（proxy 擋），真實抓取要在 VM 驗；此處鎖住不依賴網路的
契約：格式相容派網、分頁不漏不重、邊界正確。

執行：python tests/test_binance_client.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pionexbot.binance_client import BinanceClient, BinanceError  # noqa: E402
from pionexbot.smc import ohlc  # noqa: E402


class _FakeResp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    """依 (startTime, endTime, limit) 從一條合成序列切片回應，模擬幣安。"""

    def __init__(self, rows, page_limit=1000):
        self.rows = rows            # [[openTime, o,h,l,c,v,...], ...] 升冪
        self.page_limit = page_limit
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append(dict(params))
        st = params.get("startTime")
        et = params.get("endTime")
        lim = min(int(params.get("limit", 500)), self.page_limit)
        sel = self.rows
        if st is not None:
            sel = [r for r in sel if r[0] >= st]
        if et is not None:
            sel = [r for r in sel if r[0] <= et]
        # 幣安：有 startTime → 取最舊的 lim 根；只有 endTime → 取最新的 lim 根
        if st is not None:
            sel = sel[:lim]
        else:
            sel = sel[-lim:]
        return _FakeResp(sel)


def _row(t, o=1.0, h=2.0, l=0.5, c=1.5, v=10.0):  # noqa: E741
    return [t, str(o), str(h), str(l), str(c), str(v), t + 1, "0", 0, "0", "0", "0"]


# ------------------------------------------------------------------ #
# 正規化
# ------------------------------------------------------------------ #
def test_interval_mapping():
    assert BinanceClient._norm_interval("5M") == "5m"
    assert BinanceClient._norm_interval("60M") == "1h", "60M 是 1 小時、不是 60 分字面"
    assert BinanceClient._norm_interval("4H") == "4h"
    assert BinanceClient._norm_interval("1D") == "1d"
    assert BinanceClient._norm_interval("1MON") == "1M", "幣安的月才是大寫 1M"
    try:
        BinanceClient._norm_interval("7X")
        assert False, "不支援的週期應報錯"
    except BinanceError:
        pass


def test_symbol_normalization():
    assert BinanceClient._norm_symbol("BTC_USDT") == "BTCUSDT"
    assert BinanceClient._norm_symbol("eth-usdt") == "ETHUSDT"
    assert BinanceClient._norm_symbol("SOLUSDT") == "SOLUSDT"


def test_row_to_dict_is_ohlc_compatible():
    c = BinanceClient()
    d = c._to_dict(_row(1000, o=10, h=12, l=9, c=11, v=100))
    assert d == {"time": 1000, "open": 10.0, "high": 12.0,
                 "low": 9.0, "close": 11.0, "volume": 100.0}
    # 與偵測層 ohlc 擷取器相容（回測就是靠這個吃資料）
    assert ohlc.o(d) == 10.0 and ohlc.h(d) == 12.0
    assert ohlc.l(d) == 9.0 and ohlc.c(d) == 11.0 and ohlc.t(d) == 1000


# ------------------------------------------------------------------ #
# 分頁
# ------------------------------------------------------------------ #
def test_get_klines_sorts_and_shapes():
    rows = [_row(t) for t in (3000, 1000, 2000)]   # 故意亂序
    c = BinanceClient(session=_FakeSession(rows))
    out = c.get_klines("BTC_USDT", "5M", limit=10)
    assert [k["time"] for k in out] == [1000, 2000, 3000], "必須整隊成舊→新"


def test_get_klines_range_paginates_forward_no_gaps():
    step = 300_000                                  # 5M
    rows = [_row(i * step) for i in range(2500)]    # 2500 根連續
    fake = _FakeSession(rows, page_limit=1000)
    c = BinanceClient(session=fake)
    got = c.get_klines_range("BTC_USDT", "5M", 0, 2500 * step)
    times = [k["time"] for k in got]
    assert times == [i * step for i in range(2500)], "不漏不重、涵蓋整段"
    assert len(fake.calls) >= 3, "2500 根 / 每頁 1000 → 至少三頁"


def test_get_klines_range_excludes_end_boundary():
    step = 300_000
    rows = [_row(i * step) for i in range(100)]
    c = BinanceClient(session=_FakeSession(rows))
    got = c.get_klines_range("BTC_USDT", "5M", 10 * step, 20 * step)
    times = [k["time"] for k in got]
    assert times == [i * step for i in range(10, 20)], \
        "[start, end) 半開區間：含 start、不含 end"


def test_get_klines_history_backward_pagination():
    step = 300_000
    rows = [_row(i * step) for i in range(2300)]
    c = BinanceClient(session=_FakeSession(rows))
    got = c.get_klines_history("BTC_USDT", "5M", total=1500)
    assert len(got) == 1500
    assert got[-1]["time"] == 2299 * step, "回傳最近 1500 根、且以最新一根收尾"
    assert got[0]["time"] == (2300 - 1500) * step
    assert all(got[i]["time"] < got[i + 1]["time"] for i in range(len(got) - 1))


def test_retry_then_raise_on_persistent_5xx():
    class _Boom:
        def __init__(self):
            self.n = 0

        def get(self, url, params=None, timeout=None):
            self.n += 1
            return _FakeResp(None, status=503, text="down")

    import pionexbot.binance_client as bc
    orig_sleep = bc.time.sleep
    bc.time.sleep = lambda *_: None          # 別真的睡
    try:
        boom = _Boom()
        client = BinanceClient(session=boom, max_retries=3)
        try:
            client.get_klines("BTC_USDT", "5M")
            assert False, "持續 5xx 應在重試耗盡後報錯"
        except BinanceError:
            pass
        assert boom.n == 3, "應剛好重試 max_retries 次"
    finally:
        bc.time.sleep = orig_sleep


def test_client_error_not_retried():
    class _Bad:
        def __init__(self):
            self.n = 0

        def get(self, url, params=None, timeout=None):
            self.n += 1
            return _FakeResp(None, status=400, text="bad symbol")

    bad = _Bad()
    client = BinanceClient(session=bad)
    try:
        client.get_klines("NOPE", "5M")
        assert False, "4xx 應直接報錯"
    except BinanceError:
        pass
    assert bad.n == 1, "客戶端錯誤不重試"


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
