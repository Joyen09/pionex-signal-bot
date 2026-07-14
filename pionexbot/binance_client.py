"""幣安 (Binance) 公開行情客戶端——只為「長歷史回測」的資料源而生。

為什麼要它：派網 5M 只保存約 35 天（~1 萬根），兩輪回測都因樣本
< 20 筆而無法檢定 ict2022。幣安保存數年歷史，足以涵蓋
2024 多頭 / 2025 盤整 / 2026 熊市，分段檢定才有統計意義。

設計原則：介面與回傳格式對齊 `PionexClient` 的公開行情部分
（get_klines / get_klines_history 回「舊→新」的 dict list，欄位
open/high/low/close/time/volume），使 backtest_ict2022 與 SMC 偵測層
不必改一行就能吃幣安資料（規格 §1：回測與實盤共用偵測程式碼）。

只做行情（免簽章）：不下單、不碰帳戶——長歷史回測不需要，也避免
把交易權限牽扯進純研究流程。實盤永遠走派網。
"""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

from .smc.mtf import interval_ms


class BinanceError(Exception):
    """幣安 API 回傳錯誤或網路錯誤。"""


class BinanceClient:
    MAX_KLINES_PER_REQUEST = 1000   # 幣安單次上限

    # 主站 api.binance.com 對部分地區回 451（地區限制）。
    # data-api.binance.vision 是幣安官方的「公開行情專用」端點：免金鑰、
    # 無地區限制、資料與主站一致——長歷史回測的正解。全部同一份全球資料集
    # （不含 binance.us，那是不同資料集，混用會汙染回測）。
    DEFAULT_HOST = "https://data-api.binance.vision"
    FALLBACK_HOSTS = [
        "https://data-api.binance.vision",
        "https://api-gcp.binance.com",
        "https://api.binance.com",
        "https://api1.binance.com",
    ]

    # 我們的週期代號 → 幣安代號（幣安用小寫、1 小時是 1h 不是 60M）
    _INTERVAL_MAP = {
        "1M": "1m", "3M": "3m", "5M": "5m", "15M": "15m", "30M": "30m",
        "60M": "1h", "1H": "1h", "2H": "2h", "4H": "4h", "6H": "6h",
        "8H": "8h", "12H": "12h", "1D": "1d", "3D": "3d", "1W": "1w",
        "1MON": "1M",   # 幣安的「月」才是大寫 1M——別和我們的「分鐘」搞混
    }

    def __init__(self, base_url: Optional[str] = None,
                 timeout: float = 15.0,
                 session: Optional[requests.Session] = None,
                 max_retries: int = 4):
        self.base_url = (base_url or self.DEFAULT_HOST).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.max_retries = max_retries

    def _hosts(self) -> list[str]:
        """優先用指定 base_url，再依序試備援 host（去重、保順序）。"""
        seen, out = set(), []
        for h in [self.base_url] + self.FALLBACK_HOSTS:
            h = h.rstrip("/")
            if h not in seen:
                seen.add(h)
                out.append(h)
        return out

    # ------------------------------------------------------------------ #
    # 週期 / 交易對 正規化
    # ------------------------------------------------------------------ #
    @classmethod
    def _norm_interval(cls, interval: str) -> str:
        up = str(interval).strip().upper()
        if up not in cls._INTERVAL_MAP:
            raise BinanceError(f"不支援的週期：{interval}")
        return cls._INTERVAL_MAP[up]

    @staticmethod
    def _norm_symbol(symbol: str) -> str:
        """BTC_USDT → BTCUSDT（幣安現貨無底線）。已無底線則原樣大寫。"""
        return str(symbol).replace("_", "").replace("-", "").upper()

    # ------------------------------------------------------------------ #
    # 底層請求（帶指數退避重試）
    # ------------------------------------------------------------------ #
    def _get(self, path: str, params: dict[str, Any]) -> Any:
        last_exc: Optional[Exception] = None
        for host in self._hosts():
            url = host + path
            for attempt in range(self.max_retries):
                try:
                    resp = self.session.get(url, params=params,
                                            timeout=self.timeout)
                except requests.RequestException as exc:
                    last_exc = exc
                else:
                    if resp.status_code == 200:
                        return resp.json()
                    # 451/403 = 地區限制 → 別在本 host 重試，直接換下一個
                    if resp.status_code in (451, 403):
                        last_exc = BinanceError(
                            f"HTTP {resp.status_code}（{host} 地區限制）")
                        break
                    # 其餘 4xx（400 錯誤參數等）是真錯 → 直接報，不換 host
                    if resp.status_code not in (429, 418) and \
                            resp.status_code < 500:
                        raise BinanceError(
                            f"HTTP {resp.status_code}：{resp.text[:200]}")
                    # 429/418 限流、5xx 暫時性 → 退避重試同一 host
                    last_exc = BinanceError(f"HTTP {resp.status_code}")
                time.sleep(2 ** attempt)
        raise BinanceError(
            f"所有幣安端點都失敗（{last_exc}）。多半是地區限制——"
            f"可用 --base 指定可用端點，或改用其他交易所資料源。")

    # ------------------------------------------------------------------ #
    # K 線
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_dict(row: list) -> dict[str, Any]:
        """幣安陣列 [openTime, open, high, low, close, volume, closeTime, ...]
        → 與派網對齊的 dict。time 用 openTime（與我們全鏈一致：K 線時間 =
        開盤時間，收盤時間由 close_time() 另加一個週期）。"""
        return {
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }

    def get_klines(self, symbol: str, interval: str = "5M",
                   limit: int = 500, start_time: Optional[int] = None,
                   end_time: Optional[int] = None) -> list[dict[str, Any]]:
        """單次抓取（limit 上限 1000）。回傳「舊→新」dict list。

        幣安本身即回傳「舊→新」，但仍統一過一次排序，與派網客戶端行為一致。"""
        params: dict[str, Any] = {
            "symbol": self._norm_symbol(symbol),
            "interval": self._norm_interval(interval),
            "limit": min(int(limit), self.MAX_KLINES_PER_REQUEST),
        }
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        rows = self._get("/api/v3/klines", params)
        if not isinstance(rows, list):
            raise BinanceError(f"非預期回應：{str(rows)[:200]}")
        out = [self._to_dict(r) for r in rows]
        out.sort(key=lambda k: k["time"])
        return out

    def get_klines_range(self, symbol: str, interval: str,
                         start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """抓取 [start_ms, end_ms) 區間的全部 K 線：以 startTime 向前分頁。

        用於「分段回測」——把不同市場週期各切一段分別檢定。"""
        step = interval_ms(self._our_interval_span(interval))
        collected: dict[int, dict[str, Any]] = {}
        cursor = int(start_ms)
        end_ms = int(end_ms)
        # 上限防呆：區間根數 / 每頁 + 緩衝
        max_pages = (end_ms - start_ms) // (step * self.MAX_KLINES_PER_REQUEST) + 3
        for _ in range(max(1, int(max_pages))):
            if cursor >= end_ms:
                break
            page = self.get_klines(symbol, interval,
                                   self.MAX_KLINES_PER_REQUEST,
                                   start_time=cursor, end_time=end_ms)
            if not page:
                break
            for k in page:
                collected[k["time"]] = k
            newest = max(k["time"] for k in page)
            if newest < cursor:      # 沒有前進 → 停手，防無限迴圈
                break
            cursor = newest + step
            if len(page) < self.MAX_KLINES_PER_REQUEST:
                break               # 最後一頁不滿 → 抓完了
        return [collected[t] for t in sorted(collected) if t < end_ms]

    def get_klines_history(self, symbol: str, interval: str = "5M",
                           total: int = 1000) -> list[dict[str, Any]]:
        """抓最近 total 根：以 endTime 向後分頁（drop-in 對齊 PionexClient）。"""
        total = max(1, int(total))
        if total <= self.MAX_KLINES_PER_REQUEST:
            return self.get_klines(symbol, interval, total)
        collected: dict[int, dict[str, Any]] = {}
        end_time: Optional[int] = None
        max_pages = total // self.MAX_KLINES_PER_REQUEST + 3
        for _ in range(int(max_pages)):
            page = self.get_klines(symbol, interval,
                                   self.MAX_KLINES_PER_REQUEST, end_time=end_time)
            if not page:
                break
            for k in page:
                collected[k["time"]] = k
            oldest = min(k["time"] for k in page)
            new_end = oldest - 1
            if end_time is not None and new_end >= end_time:
                break
            end_time = new_end
            if len(collected) >= total or len(page) < self.MAX_KLINES_PER_REQUEST:
                break
        ordered = [collected[t] for t in sorted(collected)]
        return ordered[-total:]

    @staticmethod
    def _our_interval_span(interval: str) -> str:
        """把使用者傳的週期正規化成 interval_ms 認得的大寫代號（1H→60M）。"""
        up = str(interval).strip().upper()
        return {"1H": "60M"}.get(up, up)
