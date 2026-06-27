"""派網 (Pionex) REST API 客戶端。

簽章規則（依官方文件）：
  1. timestamp（毫秒）放入 query 參數。
  2. 將所有 query 參數依 key 的 ASCII 升冪排序，以 & 串接，
     接在 PATH 後面組成 PATH_URL：  PATH?key1=v1&key2=v2
  3. 待簽字串 = METHOD(大寫) + PATH_URL + (若有 body 則接上 body 的 JSON 字串)
  4. signature = HMAC_SHA256(secret, 待簽字串) 的十六進位小寫
  5. Header 帶上 PIONEX-KEY 與 PIONEX-SIGNATURE

重點：POST 送出的 body 必須與簽章用的 body 字串「逐字一致」，
所以這裡只序列化一次，簽章與送出共用同一個字串。

公開行情端點不需簽章。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Optional
from urllib.parse import quote

import requests


class PionexError(Exception):
    """派網 API 回傳錯誤或網路錯誤。"""


class PionexClient:
    def __init__(self, api_key: str = "", api_secret: str = "",
                 base_url: str = "https://api.pionex.com",
                 timeout: float = 10.0, session: Optional[requests.Session] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    # ------------------------------------------------------------------ #
    # 簽章
    # ------------------------------------------------------------------ #
    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _sorted_query(params: dict[str, Any]) -> str:
        """依 key ASCII 升冪排序並 url-encode。"""
        items = sorted(params.items(), key=lambda kv: kv[0])
        return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in items)

    def _sign(self, method: str, path: str, query: dict[str, Any],
              body_str: str = "") -> tuple[str, str]:
        """回傳 (path_url, signature)。"""
        sorted_qs = self._sorted_query(query)
        path_url = f"{path}?{sorted_qs}" if sorted_qs else path
        message = method.upper() + path_url + (body_str or "")
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return path_url, signature

    # ------------------------------------------------------------------ #
    # 底層請求
    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, *,
                 query: Optional[dict[str, Any]] = None,
                 body: Optional[dict[str, Any]] = None,
                 signed: bool = False) -> dict[str, Any]:
        method = method.upper()
        query = dict(query or {})
        headers = {"Content-Type": "application/json"}
        body_str = ""

        if body is not None:
            # 緊湊 JSON，簽章與送出必須用同一份字串
            body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        if signed:
            if not (self.api_key and self.api_secret):
                raise PionexError("此端點需要 API 金鑰，但尚未設定 PIONEX_API_KEY / PIONEX_API_SECRET")
            query["timestamp"] = self._now_ms()
            path_url, signature = self._sign(method, path, query, body_str)
            headers["PIONEX-KEY"] = self.api_key
            headers["PIONEX-SIGNATURE"] = signature
            url = self.base_url + path_url
        else:
            sorted_qs = self._sorted_query(query)
            url = self.base_url + path + (f"?{sorted_qs}" if sorted_qs else "")

        try:
            resp = self.session.request(
                method, url, headers=headers,
                data=body_str.encode("utf-8") if body_str else None,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise PionexError(f"網路錯誤：{exc}") from exc

        try:
            data = resp.json()
        except ValueError:
            raise PionexError(f"非 JSON 回應 (HTTP {resp.status_code})：{resp.text[:200]}")

        # 派網成功回應為 {"result": true, "data": {...}}
        if isinstance(data, dict) and data.get("result") is False:
            code = data.get("code", "")
            msg = data.get("message", data)
            raise PionexError(f"派網 API 錯誤 [{code}]：{msg}")
        if resp.status_code >= 400:
            raise PionexError(f"HTTP {resp.status_code}：{data}")
        return data

    # ------------------------------------------------------------------ #
    # 公開行情（免簽章）
    # ------------------------------------------------------------------ #
    MAX_KLINES_PER_REQUEST = 500   # 派網單次上限

    @staticmethod
    def _kline_time(k: Any) -> Optional[int]:
        if isinstance(k, dict):
            for key in ("time", "t", "timestamp", "openTime", "T"):
                if key in k:
                    return int(k[key])
        elif isinstance(k, (list, tuple)) and k:
            return int(k[0])
        return None

    def get_klines(self, symbol: str, interval: str = "5M",
                   limit: int = 100, end_time: Optional[int] = None) -> list[dict[str, Any]]:
        """取得 K 線（單次，limit 上限 500）。回傳 list，每筆含 close/time 等。"""
        q: dict[str, Any] = {
            "symbol": symbol, "interval": interval,
            "limit": min(int(limit), self.MAX_KLINES_PER_REQUEST),
        }
        if end_time is not None:
            q["endTime"] = int(end_time)
        data = self._request("GET", "/api/v1/market/klines", query=q)
        klines = data.get("data", {}).get("klines", data.get("data", []))
        return klines or []

    def get_klines_history(self, symbol: str, interval: str = "5M",
                           total: int = 1000) -> list[dict[str, Any]]:
        """抓取大量歷史 K 線：自動分頁（用 endTime 往前翻），回傳由舊到新、最多 total 根。"""
        total = max(1, int(total))
        if total <= self.MAX_KLINES_PER_REQUEST:
            return self.get_klines(symbol, interval, total)

        collected: dict[int, dict[str, Any]] = {}
        end_time: Optional[int] = None
        for _ in range(40):  # 上限 40 頁 = 2 萬根，防呆
            page = self.get_klines(symbol, interval,
                                   self.MAX_KLINES_PER_REQUEST, end_time=end_time)
            if not page:
                break
            times = [t for t in (self._kline_time(k) for k in page) if t is not None]
            if not times:
                break
            for k in page:
                t = self._kline_time(k)
                if t is not None:
                    collected[t] = k
            new_end = min(times) - 1
            if end_time is not None and new_end >= end_time:
                break  # 沒有更舊的資料了
            end_time = new_end
            if len(collected) >= total:
                break
        ordered = [collected[t] for t in sorted(collected)]
        return ordered[-total:]

    def get_ticker_price(self, symbol: str) -> float:
        """取得最新成交價（用 24h ticker 的 close）。"""
        data = self._request("GET", "/api/v1/market/tickers",
                             query={"symbol": symbol})
        tickers = data.get("data", {}).get("tickers", [])
        if not tickers:
            raise PionexError(f"找不到 {symbol} 的行情")
        t = tickers[0]
        return float(t.get("close") or t.get("price") or t.get("last"))

    def get_book_ticker(self, symbol: str) -> dict[str, float]:
        """取得最佳買賣價。"""
        data = self._request("GET", "/api/v1/market/bookTickers",
                             query={"symbol": symbol})
        tickers = data.get("data", {}).get("tickers", [])
        if not tickers:
            raise PionexError(f"找不到 {symbol} 的盤口")
        t = tickers[0]
        return {"bid": float(t["bidPrice"]), "ask": float(t["askPrice"])}

    # ------------------------------------------------------------------ #
    # 私有（需簽章）
    # ------------------------------------------------------------------ #
    def get_balances(self) -> dict[str, float]:
        """取得各幣種可用餘額，回傳 {coin: free}。"""
        data = self._request("GET", "/api/v1/account/balances", signed=True)
        balances = data.get("data", {}).get("balances", [])
        return {b["coin"]: float(b.get("free", 0)) for b in balances}

    def place_order(self, symbol: str, side: str, order_type: str = "MARKET",
                    size: Optional[float | str] = None,
                    amount: Optional[float | str] = None,
                    price: Optional[float | str] = None,
                    ioc: Optional[bool] = None,
                    client_order_id: Optional[str] = None) -> dict[str, Any]:
        """下單。POST /api/v1/trade/order

        市價買入：用 amount（要花多少報價幣，如 USDT）
        市價賣出：用 size（要賣多少基礎幣）
        限價單：需 size 與 price
        """
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": order_type.upper(),
        }
        if size is not None:
            body["size"] = str(size)
        if amount is not None:
            body["amount"] = str(amount)
        if price is not None:
            body["price"] = str(price)
        if ioc is not None:
            body["IOC"] = bool(ioc)
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._request("POST", "/api/v1/trade/order", body=body, signed=True)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", "/api/v1/trade/order",
                             query={"orderId": order_id}, signed=True)

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        return self._request("DELETE", "/api/v1/trade/order",
                             body={"symbol": symbol, "orderId": order_id}, signed=True)

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/v1/trade/openOrders",
                             query={"symbol": symbol}, signed=True)
        return data.get("data", {}).get("orders", [])
