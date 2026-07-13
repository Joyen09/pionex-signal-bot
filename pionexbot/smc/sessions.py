"""時段（Killzone）：判斷時間屬於哪個時段、記錄各時段當日高低作為流動性池。

時區用 IANA 字串（zoneinfo 自動處理夏冬令）；跨日時段（如 asia 20:00-00:00）
以「起 > 迄」表示，判斷時繞過午夜。
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import ohlc
from .types import LiquidityPool, PoolSide, PoolSource, SwingKind, SwingPoint

DEFAULT_SESSIONS = {
    "tz": "America/New_York",
    "asia": ["20:00", "00:00"],
    "london": ["03:00", "06:00"],
    "newyork": ["09:00", "11:00"],
}


class SessionClock:
    def __init__(self, cfg: Optional[dict] = None):
        cfg = {**DEFAULT_SESSIONS, **(cfg or {})}
        self.tz = ZoneInfo(str(cfg.get("tz", "America/New_York")))
        self.windows: dict[str, tuple[dtime, dtime]] = {}
        for name in ("asia", "london", "newyork"):
            win = cfg.get(name)
            if not win or len(win) != 2:
                continue
            self.windows[name] = (_parse_hhmm(win[0]), _parse_hhmm(win[1]))

    def in_killzone(self, ts_ms: float) -> Optional[str]:
        """回傳時間戳（毫秒 UTC）所屬時段名稱；不在任何時段回 None。"""
        local = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) \
            .astimezone(self.tz).time()
        for name, (start, end) in self.windows.items():
            if start <= end:
                hit = start <= local < end
            else:  # 跨午夜（asia 20:00 → 00:00）
                hit = local >= start or local < end
            if hit:
                return name
        return None

    def session_pools(self, klines) -> list[LiquidityPool]:
        """各時段「每一次出現」的高低點 → 流動性池（created_at = 時段結束後首根）。"""
        pools: list[LiquidityPool] = []
        cur: Optional[str] = None
        hi = lo = None
        start_idx = 0
        for i, k in enumerate(klines):
            ts = ohlc.t(k)
            if ts is None:
                return []
            name = self.in_killzone(float(ts))
            if name != cur:
                if cur is not None and hi is not None:
                    pools += _session_pair(cur, hi, lo, start_idx, i)
                cur, hi, lo, start_idx = name, None, None, i
            if name is not None:
                hi = ohlc.h(k) if hi is None else max(hi, ohlc.h(k))
                lo = ohlc.l(k) if lo is None else min(lo, ohlc.l(k))
        return pools


def _session_pair(name: str, hi: float, lo: float,
                  start_idx: int, end_idx: int) -> list[LiquidityPool]:
    hi_sp = SwingPoint(start_idx, None, hi, SwingKind.HIGH, end_idx)
    lo_sp = SwingPoint(start_idx, None, lo, SwingKind.LOW, end_idx)
    return [
        LiquidityPool(hi, PoolSide.BUY_SIDE, PoolSource.SESSION,
                      [hi_sp], created_at=end_idx),
        LiquidityPool(lo, PoolSide.SELL_SIDE, PoolSource.SESSION,
                      [lo_sp], created_at=end_idx),
    ]


def _parse_hhmm(s: str) -> dtime:
    hh, mm = str(s).split(":")
    return dtime(int(hh), int(mm))
