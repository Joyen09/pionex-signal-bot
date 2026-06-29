"""DCA 定期定額（含逢低加碼）核心邏輯與回測。

逢低加碼：照常每隔一段時間買，但價格低於「參考均線」越多，買越多。
  跌幅 = (參考均線 - 現價) / 參考均線
  檔位 = floor(跌幅 / dip_step)
  倍數 = min(1 + 檔位 × mult_per_step, max_mult)
  買入金額 = quote_base × 倍數
價格高於均線時就買基本額（倍數 1）。

只買、累積、長抱（不賣）。適合「相信長期上漲」的資產。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .grid import _ohlc


def smart_amount(price: float, reference: Optional[float], base: float,
                 dip_step: float, mult_per_step: float, max_mult: float) -> tuple[float, float]:
    """回傳 (買入金額, 倍數)。reference 為 None 或價格不低於它 → 倍數 1。"""
    mult = 1.0
    if reference and reference > 0 and price < reference and dip_step > 0:
        dev = (reference - price) / reference
        steps = int(dev / dip_step + 1e-9)   # +epsilon 避免浮點誤差少算一檔
        mult = min(1 + steps * mult_per_step, max_mult)
    return base * mult, mult


@dataclass
class DcaResult:
    label: str
    invested: float       # 總投入(USDT)
    base_acc: float       # 累積買到的基礎幣
    avg_cost: float       # 平均成本
    final_price: float
    final_value: float    # 期末市值
    buys: int

    @property
    def total_return(self) -> float:
        return (self.final_value - self.invested) / self.invested if self.invested else 0.0


def backtest_dca(klines: list[dict[str, Any]], *, base: float = 10.0,
                 every: int = 24, ref_period: int = 20, dip_step: float = 0.05,
                 mult_per_step: float = 0.5, max_mult: float = 3.0,
                 smart: bool = True, label: str = "") -> DcaResult:
    closes = [_ohlc(k)[2] for k in klines]
    invested = base_acc = 0.0
    buys = 0
    for i in range(0, len(closes), max(1, every)):
        price = closes[i]
        if smart and i >= ref_period:
            ref = sum(closes[i - ref_period:i]) / ref_period
        else:
            ref = None
        amount, _ = smart_amount(price, ref, base, dip_step, mult_per_step, max_mult)
        invested += amount
        base_acc += amount / price
        buys += 1
    final_price = closes[-1]
    avg_cost = invested / base_acc if base_acc else 0.0
    return DcaResult(label=label or ("逢低加碼" if smart else "陽春定額"),
                     invested=invested, base_acc=base_acc, avg_cost=avg_cost,
                     final_price=final_price, final_value=base_acc * final_price,
                     buys=buys)


def compare_dca(klines: list[dict[str, Any]], **kw) -> list[DcaResult]:
    """同一段資料比較『陽春定額』與『逢低加碼』。"""
    return [
        backtest_dca(klines, smart=False, label="陽春定額", **kw),
        backtest_dca(klines, smart=True, label="逢低加碼", **kw),
    ]
