"""網格交易 (Grid Trading) 引擎與回測。

概念：在 [lower, upper] 價格區間內均勻畫出 N 條網格線。
每條線掛一張買單；買單成交後，在上一條線掛賣單。
價格在區間內來回震盪時，就能不斷「低買、高賣」賺取網格間距的利潤。

罩門：價格若「跌破區間下緣」，手上會累積一堆高價買進的貨（套牢），
這時未實現虧損會很大——所以回測會把「已實現獲利」與「未實現套牢」分開呈現。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _ohlc(k: Any) -> tuple[float, float, float]:
    """取出 (high, low, close)，欄位缺失時退回 close。"""
    if isinstance(k, dict):
        c = float(k.get("close", k.get("c")))
        h = k.get("high", k.get("h"))
        low = k.get("low", k.get("l"))
        h = float(h) if h is not None else c
        low = float(low) if low is not None else c
        return h, low, c
    # list 格式：[time, open, high, low, close, volume]
    return float(k[2]), float(k[3]), float(k[4])


@dataclass
class GridResult:
    symbol: str
    lower: float
    upper: float
    grids: int
    start_cash: float
    end_equity: float
    realized_profit: float    # 已完成「買→賣」實現的利潤
    unrealized: float         # 期末仍持有的存貨，以期末價計算的未實現損益
    completed_grids: int      # 完成的網格來回次數
    buys: int
    sells: int
    final_price: float
    buy_hold_return: float

    @property
    def total_return(self) -> float:
        return (self.end_equity - self.start_cash) / self.start_cash if self.start_cash else 0.0

    def summary(self) -> str:
        return "\n".join([
            "════════ 網格回測結果 ════════",
            f"  交易對        : {self.symbol}",
            f"  區間          : {self.lower:.2f} ~ {self.upper:.2f}（{self.grids} 格）",
            f"  起始資金      : {self.start_cash:.2f}",
            f"  期末總權益    : {self.end_equity:.2f}",
            f"  總報酬        : {self.total_return * 100:+.2f}%",
            f"  ├ 已實現網格利潤: {self.realized_profit:+.2f}（完成 {self.completed_grids} 次來回）",
            f"  └ 未實現(套牢)  : {self.unrealized:+.2f}",
            f"  買入 / 賣出次數: {self.buys} / {self.sells}",
            f"  期末價格      : {self.final_price:.2f}",
            f"  同期買入持有  : {self.buy_hold_return * 100:+.2f}%",
            "══════════════════════════════",
        ])


class GridBacktester:
    def __init__(self, lower: float, upper: float, grids: int,
                 quote_per_grid: float, fee_rate: float = 0.0005,
                 start_cash: float | None = None):
        if lower >= upper:
            raise ValueError("lower 必須小於 upper")
        if grids < 2:
            raise ValueError("grids 至少 2")
        self.lower = lower
        self.upper = upper
        self.grids = grids
        self.quote_per_grid = quote_per_grid
        self.fee_rate = fee_rate
        # 預設準備足夠買滿整個網格的資金
        self.start_cash = start_cash if start_cash is not None else quote_per_grid * grids

    def run(self, klines: list[dict[str, Any]], symbol: str) -> GridResult:
        n = self.grids
        step = (self.upper - self.lower) / n
        levels = [self.lower + i * step for i in range(n + 1)]  # n+1 條線

        cash = self.start_cash
        held: dict[int, float] = {}   # 在第 i 條線買進、尚未賣出的數量
        realized = 0.0
        buys = sells = completed = 0

        ohlc = [_ohlc(k) for k in klines]
        if not ohlc:
            raise ValueError("沒有 K 線資料")
        first_close = ohlc[0][2]
        prev = first_close   # 上一根收盤，用來判斷「穿越」

        for high, low, close in ohlc:
            for i in range(n):           # 第 i 條線掛買、第 i+1 條線掛賣
                buy_px = levels[i]
                sell_px = levels[i + 1]
                # 賣出：之前在下方，這根高點向上穿過上一格
                if i in held and prev < sell_px <= high:
                    qty = held.pop(i)
                    proceeds = qty * sell_px * (1 - self.fee_rate)
                    cost = qty * buy_px        # 當初的買入名目
                    cash += proceeds
                    realized += proceeds - cost
                    sells += 1
                    completed += 1
                # 買入：之前在上方，這根低點向下穿過該格、且資金足夠
                if i not in held and prev > buy_px >= low and cash >= self.quote_per_grid:
                    spend = self.quote_per_grid
                    qty = (spend - spend * self.fee_rate) / buy_px
                    cash -= spend
                    held[i] = qty
                    buys += 1
            prev = close

        final_price = ohlc[-1][2]
        inventory_qty = sum(held.values())
        inventory_value = inventory_qty * final_price
        # 未實現 = 存貨現值 - 存貨買入成本
        inventory_cost = sum(q * levels[i] for i, q in held.items())
        unrealized = inventory_value - inventory_cost
        end_equity = cash + inventory_value
        buy_hold = (final_price - first_close) / first_close if first_close else 0.0

        return GridResult(
            symbol=symbol, lower=self.lower, upper=self.upper, grids=self.grids,
            start_cash=self.start_cash, end_equity=end_equity,
            realized_profit=realized, unrealized=unrealized,
            completed_grids=completed, buys=buys, sells=sells,
            final_price=final_price, buy_hold_return=buy_hold,
        )
