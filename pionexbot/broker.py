"""下單介面：紙上 (PaperBroker) 與 實盤 (LiveBroker) 共用同一組方法。

兩者都實作：
    get_price(symbol) -> float
    market_buy(symbol, quote_amount) -> OrderResult
    market_sell(symbol, base_size)   -> OrderResult
    limit_buy / limit_sell

executor 只認介面，不在意背後是模擬還是真錢，方便切換。
"""
from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from typing import Optional

from .models import OrderResult, Side
from .pionex_client import PionexClient, PionexError


class Broker(ABC):
    simulated: bool

    @abstractmethod
    def get_price(self, symbol: str) -> float: ...

    @abstractmethod
    def market_buy(self, symbol: str, quote_amount: float) -> OrderResult: ...

    @abstractmethod
    def market_sell(self, symbol: str, base_size: float) -> OrderResult: ...

    def limit_buy(self, symbol: str, base_size: float, price: float) -> OrderResult:
        raise NotImplementedError

    def limit_sell(self, symbol: str, base_size: float, price: float) -> OrderResult:
        raise NotImplementedError


class LiveBroker(Broker):
    """真實下單，透過派網 API。"""

    simulated = False

    def __init__(self, client: PionexClient):
        self.client = client
        self._prec_cache: dict[str, int] = {}

    def get_price(self, symbol: str) -> float:
        return self.client.get_ticker_price(symbol)

    def _base_precision(self, symbol: str) -> int:
        """查詢交易對的基礎幣數量精度，查不到時用保守的 6 位。"""
        if symbol not in self._prec_cache:
            prec = 6
            try:
                info = self.client.get_symbol_info(symbol) or {}
                for key in ("basePrecision", "quantityPrecision", "sizePrecision"):
                    if key in info:
                        prec = int(info[key])
                        break
            except Exception:  # noqa: BLE001 - 查不到規格就用保守預設
                pass
            self._prec_cache[symbol] = prec
        return self._prec_cache[symbol]

    def _fmt_size(self, symbol: str, size: float) -> str:
        """賣出數量依交易對精度「無條件捨去」並輸出固定小數字串。

        str(float) 的長尾（如 0.000198990637194）會被 API 的 size filter 拒單；
        捨去而非四捨五入，避免賣超過實際持有量。"""
        prec = self._base_precision(symbol)
        q = 10 ** prec
        truncated = math.floor(size * q + 1e-9) / q
        s = f"{truncated:.{prec}f}".rstrip("0").rstrip(".")
        return s or "0"

    @staticmethod
    def _extract_fill(d: dict) -> tuple[float, float]:
        """從訂單物件抓出 (已成交基礎幣, 已成交報價幣)。

        只認「成交」欄位——size/amount 是下單參數，訂單還沒成交時也存在，
        絕不能當成交量；市價單的 price 欄位是 0，也不能當成交均價。"""
        base_keys = ("filledSize", "filledBase", "executedQty",
                     "fillSize", "cumBase", "tradedBase")
        quote_keys = ("filledAmount", "filledQuote", "cumQuote", "tradedQuote",
                      "fillAmount")
        def pick(keys):
            for k in keys:
                v = d.get(k)
                if v in (None, ""):
                    continue
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if f > 0:
                    return f
            return 0.0
        fb, fq = pick(base_keys), pick(quote_keys)
        if fb and not fq:  # 只有數量沒有金額 → 用「大於 0 的」均價欄位推算
            for pk in ("filledPrice", "averagePrice", "avgPrice"):
                try:
                    p = float(d.get(pk) or 0)
                except (TypeError, ValueError):
                    p = 0.0
                if p > 0:
                    fq = fb * p
                    break
        return fb, fq

    def _parse_fill(self, resp: dict, side: Side, symbol: str) -> OrderResult:
        data = resp.get("data", {}) or {}
        order_id = str(data.get("orderId", data.get("id", "")))
        filled_base, filled_quote = self._extract_fill(data)

        # 市價單的下單回應通常只有 orderId、不含成交明細 → 查詢該訂單取得實際成交
        # 注意：成交欄位可能延遲更新，要輪詢等到出現或訂單結束為止
        if filled_base <= 0 and order_id:
            for _ in range(6):
                try:
                    od = self.client.get_order(order_id).get("data", {}) or {}
                except PionexError:
                    break
                fb, fq = self._extract_fill(od)
                status = str(od.get("status", "")).upper()
                if fb > 0:
                    filled_base, filled_quote, data = fb, fq, od
                    break
                if status in ("CLOSED", "FILLED", "COMPLETED"):
                    # 已結束但成交欄位仍空：市價單「結束＝已成交」，退用下單參數
                    def _f(key):
                        try:
                            return float(od.get(key, 0) or 0)
                        except (TypeError, ValueError):
                            return 0.0
                    filled_base, filled_quote, data = _f("size"), _f("amount"), od
                    break
                if status in ("CANCELED", "CANCELLED", "REJECTED"):
                    data = od
                    break
                time.sleep(0.5)

        # 最後保險：只有單邊數字（算不出均價）→ 用現價回推，避免記帳出現 0 價
        if filled_base > 0 and filled_quote <= 0:
            try:
                filled_quote = filled_base * self.client.get_ticker_price(symbol)
            except PionexError:
                pass
        elif filled_quote > 0 and filled_base <= 0:
            try:
                px = self.client.get_ticker_price(symbol)
                if px > 0:
                    filled_base = filled_quote / px
            except PionexError:
                pass

        avg = filled_quote / filled_base if filled_base else 0.0
        return OrderResult(
            ok=True, side=side, symbol=symbol, simulated=False,
            filled_base=filled_base, filled_quote=filled_quote,
            avg_price=avg, order_id=order_id, raw=data,
        )

    def market_buy(self, symbol: str, quote_amount: float) -> OrderResult:
        try:
            resp = self.client.place_order(symbol, "BUY", "MARKET", amount=quote_amount)
            return self._parse_fill(resp, Side.BUY, symbol)
        except PionexError as exc:
            return OrderResult(ok=False, side=Side.BUY, symbol=symbol, error=str(exc))

    def market_sell(self, symbol: str, base_size: float) -> OrderResult:
        try:
            resp = self.client.place_order(symbol, "SELL", "MARKET",
                                           size=self._fmt_size(symbol, base_size))
            return self._parse_fill(resp, Side.SELL, symbol)
        except PionexError as exc:
            return OrderResult(ok=False, side=Side.SELL, symbol=symbol, error=str(exc))

    def limit_buy(self, symbol: str, base_size: float, price: float) -> OrderResult:
        try:
            resp = self.client.place_order(symbol, "BUY", "LIMIT",
                                           size=self._fmt_size(symbol, base_size), price=price)
            return self._parse_fill(resp, Side.BUY, symbol)
        except PionexError as exc:
            return OrderResult(ok=False, side=Side.BUY, symbol=symbol, error=str(exc))

    def limit_sell(self, symbol: str, base_size: float, price: float) -> OrderResult:
        try:
            resp = self.client.place_order(symbol, "SELL", "LIMIT",
                                           size=self._fmt_size(symbol, base_size), price=price)
            return self._parse_fill(resp, Side.SELL, symbol)
        except PionexError as exc:
            return OrderResult(ok=False, side=Side.SELL, symbol=symbol, error=str(exc))


class PaperBroker(Broker):
    """紙上交易：用真實市價模擬成交，但不送出任何真實訂單。"""

    simulated = True

    def __init__(self, client: Optional[PionexClient] = None, fee_rate: float = 0.0005):
        self.client = client
        self.fee_rate = fee_rate
        self._fake_id = 0
        self._last_price_cache: dict[str, float] = {}

    def get_price(self, symbol: str) -> float:
        if self.client is not None:
            try:
                price = self.client.get_ticker_price(symbol)
                self._last_price_cache[symbol] = price
                return price
            except PionexError:
                pass
        # 無法取得行情時退回上次快取（離線測試用）
        return self._last_price_cache.get(symbol, 0.0)

    def _next_id(self) -> str:
        self._fake_id += 1
        return f"paper-{self._fake_id}"

    def market_buy(self, symbol: str, quote_amount: float) -> OrderResult:
        price = self.get_price(symbol)
        if price <= 0:
            return OrderResult(ok=False, side=Side.BUY, symbol=symbol,
                               simulated=True, error="取不到市價，無法模擬")
        fee = quote_amount * self.fee_rate
        base = (quote_amount - fee) / price
        return OrderResult(ok=True, side=Side.BUY, symbol=symbol, simulated=True,
                           filled_base=base, filled_quote=quote_amount,
                           avg_price=price, order_id=self._next_id())

    def market_sell(self, symbol: str, base_size: float) -> OrderResult:
        price = self.get_price(symbol)
        if price <= 0:
            return OrderResult(ok=False, side=Side.SELL, symbol=symbol,
                               simulated=True, error="取不到市價，無法模擬")
        gross = base_size * price
        quote = gross * (1 - self.fee_rate)
        return OrderResult(ok=True, side=Side.SELL, symbol=symbol, simulated=True,
                           filled_base=base_size, filled_quote=quote,
                           avg_price=price, order_id=self._next_id())
