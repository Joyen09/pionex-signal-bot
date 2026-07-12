"""核心資料結構。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    CLOSE = "CLOSE"   # 平倉（賣出目前全部持倉）
    HOLD = "HOLD"     # 不動作


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass
class TakeProfit:
    """一段停利：price 觸價即賣出「原始倉位 × fraction」。

    同一 Signal 內所有 fraction 總和必須 <= 1。"""

    price: float
    fraction: float


@dataclass
class Signal:
    """一個標準化的交易訊號，不論來自策略或 webhook 都轉成這個。"""

    action: Action
    symbol: str
    source: str = "unknown"          # strategy:ma_cross / webhook:tradingview ...
    quote_amount: Optional[float] = None   # 買入時要花的報價幣金額（USDT）
    base_size: Optional[float] = None      # 賣出時要賣的基礎幣數量
    price: Optional[float] = None          # 限價單參考價（市價單可省略）
    reason: str = ""                  # 人類可讀的觸發原因
    raw: dict[str, Any] = field(default_factory=dict)  # 原始 payload
    # --- 以止損決定倉位（皆為選填；未帶時走原本 quote_per_trade 流程）---
    stop_loss: Optional[float] = None            # 止損價（做多需低於進場價）
    take_profits: Optional[list[TakeProfit]] = None  # 多段停利
    risk_quote: Optional[float] = None           # 本筆願意虧的 USDT
    tags: Optional[dict[str, Any]] = None        # 策略 setup 資訊，寫入 SQLite 供回測分析

    def __str__(self) -> str:
        bits = [f"{self.action.value} {self.symbol}"]
        if self.quote_amount is not None:
            bits.append(f"金額={self.quote_amount}")
        if self.base_size is not None:
            bits.append(f"數量={self.base_size}")
        if self.stop_loss is not None:
            bits.append(f"SL={self.stop_loss}")
        if self.take_profits:
            bits.append("TP=" + "/".join(f"{tp.price}x{tp.fraction}"
                                         for tp in self.take_profits))
        if self.reason:
            bits.append(f"({self.reason})")
        bits.append(f"來源={self.source}")
        return " ".join(bits)


@dataclass
class OrderResult:
    """下單結果（紙上或實盤共用）。"""

    ok: bool
    side: Side
    symbol: str
    filled_base: float = 0.0     # 成交的基礎幣數量
    filled_quote: float = 0.0    # 成交的報價幣金額
    avg_price: float = 0.0       # 平均成交價
    order_id: str = ""
    simulated: bool = False      # 是否為紙上模擬
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        tag = "模擬" if self.simulated else "實盤"
        if not self.ok:
            return f"[{tag}] 下單失敗 {self.side.value} {self.symbol}: {self.error}"
        return (f"[{tag}] {self.side.value} {self.symbol} 成交 "
                f"{self.filled_base:.8f} @ {self.avg_price:.2f} "
                f"= {self.filled_quote:.2f} (id={self.order_id})")
