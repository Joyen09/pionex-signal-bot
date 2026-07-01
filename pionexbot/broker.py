"""下單介面：紙上 (PaperBroker) 與 實盤 (LiveBroker) 共用同一組方法。

兩者都實作：
    get_price(symbol) -> float
    market_buy(symbol, quote_amount) -> OrderResult
    market_sell(symbol, base_size)   -> OrderResult
    limit_buy / limit_sell

executor 只認介面，不在意背後是模擬還是真錢，方便切換。
"""
from __future__ import annotations

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

    def get_price(self, symbol: str) -> float:
        return self.client.get_ticker_price(symbol)

    @staticmethod
    def _extract_fill(d: dict) -> tuple[float, float]:
        """從訂單物件盡量抓出 (已成交基礎幣, 已成交報價幣)，容忍不同欄位命名。"""
        base_keys = ("filledSize", "filledBase", "executedQty", "filled",
                     "fillSize", "cumBase", "tradedBase", "size")
        quote_keys = ("filledAmount", "filledQuote", "cumQuote", "tradedQuote",
                      "fillAmount", "amount")
        def pick(keys):
            for k in keys:
                v = d.get(k)
                if v not in (None, "", "0", 0):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return 0.0
        fb, fq = pick(base_keys), pick(quote_keys)
        if fb and not fq:  # 只有數量沒有金額 → 用均價推算
            for pk in ("filledPrice", "averagePrice", "avgPrice", "price"):
                if d.get(pk):
                    fq = fb * float(d[pk])
                    break
        return fb, fq

    def _parse_fill(self, resp: dict, side: Side, symbol: str) -> OrderResult:
        data = resp.get("data", {}) or {}
        order_id = str(data.get("orderId", data.get("id", "")))
        filled_base, filled_quote = self._extract_fill(data)

        # 市價單的下單回應通常只有 orderId、不含成交明細 → 查詢該訂單取得實際成交
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
                if status in ("CLOSED", "FILLED", "COMPLETED", "CANCELED",
                              "CANCELLED", "REJECTED"):
                    data = od
                    break
                time.sleep(0.5)

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
            resp = self.client.place_order(symbol, "SELL", "MARKET", size=base_size)
            return self._parse_fill(resp, Side.SELL, symbol)
        except PionexError as exc:
            return OrderResult(ok=False, side=Side.SELL, symbol=symbol, error=str(exc))

    def limit_buy(self, symbol: str, base_size: float, price: float) -> OrderResult:
        try:
            resp = self.client.place_order(symbol, "BUY", "LIMIT", size=base_size, price=price)
            return self._parse_fill(resp, Side.BUY, symbol)
        except PionexError as exc:
            return OrderResult(ok=False, side=Side.BUY, symbol=symbol, error=str(exc))

    def limit_sell(self, symbol: str, base_size: float, price: float) -> OrderResult:
        try:
            resp = self.client.place_order(symbol, "SELL", "LIMIT", size=base_size, price=price)
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
