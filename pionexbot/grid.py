"""網格交易 (Grid Trading) 引擎與回測。

概念：在 [lower, upper] 價格區間內均勻畫出 N 條網格線。
每條線掛一張買單；買單成交後，在上一條線掛賣單。
價格在區間內來回震盪時，就能不斷「低買、高賣」賺取網格間距的利潤。

罩門：價格若「跌破區間下緣」，手上會累積一堆高價買進的貨（套牢），
這時未實現虧損會很大——所以回測會把「已實現獲利」與「未實現套牢」分開呈現。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


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


# ============================================================================
# 動態網格回測：與 live GridRunner 同邏輯（ATR 區間 + ADX 過濾 + 跌破自動重開）
# ============================================================================
@dataclass
class DynGridResult:
    label: str
    symbol: str
    bars: int
    start_cash: float
    end_equity: float
    realized: float
    unrealized: float
    buys: int
    sells: int
    completed: int
    resets: int
    paused_bars: int
    max_drawdown: float
    final_price: float
    buy_hold_return: float

    @property
    def total_return(self) -> float:
        return (self.end_equity - self.start_cash) / self.start_cash if self.start_cash else 0.0


class DynamicGridBacktester:
    """回放與 live GridRunner 相同的決策邏輯。

    use_atr   : 區間用 ATR 動態（否則用 range_pct 固定）
    use_regime: ADX 強趨勢時暫停開網格
    use_reset : 跌破/突破區間時關閉並重開（動態網格）；False 則固定不重開
    """

    def __init__(self, *, grids: int = 20, quote_per_grid: float = 50.0,
                 fee_rate: float = 0.0005, start_cash: Optional[float] = None,
                 use_atr: bool = True, range_pct: float = 0.05,
                 atr_period: int = 14, atr_mult: float = 6.0,
                 use_regime: bool = True, adx_period: int = 14, adx_max: float = 30.0,
                 use_reset: bool = True, breakout_buffer: float = 0.02,
                 indicator_interval_ms: Optional[int] = None):
        self.grids = grids
        self.quote_per_grid = quote_per_grid
        self.fee_rate = fee_rate
        self.start_cash = start_cash if start_cash is not None else quote_per_grid * grids
        self.use_atr = use_atr
        self.range_pct = range_pct
        self.atr_period = atr_period
        self.atr_mult = atr_mult
        self.use_regime = use_regime
        self.adx_period = adx_period
        self.adx_max = adx_max
        self.use_reset = use_reset
        self.breakout_buffer = breakout_buffer
        # 指標時框（毫秒）：live GridRunner 用 indicator_interval（預設 60M）算
        # ATR/ADX，而非資料時框。回測若直接在 15M 上算，ATR 偏小 → 區間偏窄 →
        # 重開/手續費被灌水，對網格系統性偏悲觀。None = 沿用資料時框（舊行為）。
        self.indicator_interval_ms = indicator_interval_ms

    def _levels(self, lower: float, upper: float) -> list[float]:
        step = (upper - lower) / self.grids
        return [lower + i * step for i in range(self.grids + 1)]

    def _indicator_series(self, klines, ohlc):
        """回傳逐資料根的 (atr, adx) 兩個 list。

        indicator_interval_ms 有給且 K 線帶時間戳時：先聚合成指標時框
        （與 live 的 60M 一致），每根資料 K 只用「已收盤」的聚合 K 取值
        （防前視）；否則直接在資料時框上算（舊行為）。"""
        import pandas as pd
        from .strategy import indicators

        n = len(ohlc)
        times = [k.get("time") if isinstance(k, dict) else
                 (k[0] if isinstance(k, (list, tuple)) and k else None)
                 for k in klines]
        step = self.indicator_interval_ms
        if not step or any(t is None for t in times):
            highs = pd.Series([o[0] for o in ohlc])
            lows = pd.Series([o[1] for o in ohlc])
            closes = pd.Series([o[2] for o in ohlc])
            atr_s = indicators.atr(highs, lows, closes, self.atr_period)
            adx_s = indicators.adx(highs, lows, closes, self.adx_period)
            return list(atr_s), list(adx_s)

        # 聚合到指標時框（依 time // step 分桶，桶內取 高=max/低=min/收=末）
        buckets: list[list[float]] = []      # [h, l, c]
        bucket_ids: list[int] = []
        bar_bucket_pos: list[int] = []       # 每根資料 K 落在第幾個桶
        for t, (h, low, c) in zip(times, ohlc):
            b = int(float(t)) // int(step)
            if not bucket_ids or b != bucket_ids[-1]:
                bucket_ids.append(b)
                buckets.append([h, low, c])
            else:
                buckets[-1][0] = max(buckets[-1][0], h)
                buckets[-1][1] = min(buckets[-1][1], low)
                buckets[-1][2] = c
            bar_bucket_pos.append(len(buckets) - 1)
        bh = pd.Series([b[0] for b in buckets])
        bl = pd.Series([b[1] for b in buckets])
        bc = pd.Series([b[2] for b in buckets])
        atr_b = list(indicators.atr(bh, bl, bc, self.atr_period))
        adx_b = list(indicators.adx(bh, bl, bc, self.adx_period))
        nan = float("nan")
        # 只用「上一個已收盤」的桶（目前所在桶還沒收完）
        atr = [atr_b[p - 1] if p >= 1 else nan for p in bar_bucket_pos]
        adx = [adx_b[p - 1] if p >= 1 else nan for p in bar_bucket_pos]
        return atr, adx

    def run(self, klines: list[dict[str, Any]], symbol: str, label: str = "") -> DynGridResult:
        ohlc = [_ohlc(k) for k in klines]
        n = len(ohlc)
        atr_list, adx_list = self._indicator_series(klines, ohlc)
        import pandas as pd
        closes = pd.Series([o[2] for o in ohlc])
        atr_s = pd.Series(atr_list)
        adx_s = pd.Series(adx_list)

        cash = self.start_cash
        active = False
        lower = upper = 0.0
        levels: list[float] = []
        held: dict[int, float] = {}
        last_price = closes.iloc[0]
        realized = 0.0
        buys = sells = completed = resets = paused = 0
        peak = self.start_cash
        max_dd = 0.0

        def equity(price: float) -> float:
            return cash + sum(q * price for q in held.values())

        def is_ranging(i: int) -> bool:
            if not self.use_regime:
                return True
            v = adx_s.iloc[i]
            return True if v != v else float(v) < self.adx_max  # NaN→True

        def bounds(i: int, price: float):
            if self.use_atr:
                a = atr_s.iloc[i]
                if a == a and a > 0:
                    half = self.atr_mult * float(a)
                    return price - half, price + half
                return None  # ATR 還沒準備好
            return price * (1 - self.range_pct), price * (1 + self.range_pct)

        def open_grid(i: int, price: float) -> bool:
            nonlocal active, lower, upper, levels, held, last_price
            b = bounds(i, price)
            if b is None:
                return False
            lower, upper = b
            levels = self._levels(lower, upper)
            held = {}
            last_price = price
            active = True
            return True

        def close_grid(price: float):
            nonlocal cash, held, active
            for j, q in held.items():
                cash += q * price * (1 - self.fee_rate)
            held = {}
            active = False

        for i in range(n):
            high, low, close = ohlc[i]
            if not active:
                if is_ranging(i):
                    open_grid(i, close)
                else:
                    paused += 1
                last_price = close
                # 紀錄權益
                peak = max(peak, equity(close))
                if peak > 0:
                    max_dd = max(max_dd, (peak - equity(close)) / peak)
                continue

            # 風險：跌破下緣 → 關閉（動態才重開）
            if close < lower * (1 - self.breakout_buffer):
                close_grid(close)
                resets += 1
                if self.use_reset and is_ranging(i):
                    open_grid(i, close)
                last_price = close
            elif close > upper * (1 + self.breakout_buffer):
                close_grid(close)
                resets += 1
                if self.use_reset and is_ranging(i):
                    open_grid(i, close)
                last_price = close
            else:
                # 正常穿越成交
                for j in range(self.grids):
                    buy_px, sell_px = levels[j], levels[j + 1]
                    if j in held and last_price < sell_px <= high:
                        q = held.pop(j)
                        cash += q * sell_px * (1 - self.fee_rate)
                        realized += q * (sell_px - buy_px) - q * sell_px * self.fee_rate
                        sells += 1
                        completed += 1
                    if j not in held and last_price > buy_px >= low and cash >= self.quote_per_grid:
                        spend = self.quote_per_grid
                        q = (spend - spend * self.fee_rate) / buy_px
                        cash -= spend
                        held[j] = q
                        buys += 1
                last_price = close

            eq = equity(close)
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)

        final_price = closes.iloc[-1]
        inv_value = sum(q * final_price for q in held.values())
        inv_cost = sum(q * levels[j] for j, q in held.items()) if levels else 0.0
        unreal = inv_value - inv_cost
        end_equity = cash + inv_value
        bh = (final_price - closes.iloc[0]) / closes.iloc[0] if closes.iloc[0] else 0.0

        return DynGridResult(
            label=label or "dyn", symbol=symbol, bars=n,
            start_cash=self.start_cash, end_equity=end_equity,
            realized=realized, unrealized=unreal, buys=buys, sells=sells,
            completed=completed, resets=resets, paused_bars=paused,
            max_drawdown=max_dd, final_price=final_price, buy_hold_return=bh,
        )


def compare_grid_variants(klines: list[dict[str, Any]], symbol: str,
                          grids: int = 20, start_cash: float = 1000.0,
                          **kw) -> list[DynGridResult]:
    """同一段資料比較三種網格：陽春固定 / 動態(ATR+重開) / 動態+ADX過濾。"""
    qpg = start_cash / grids
    common = dict(grids=grids, quote_per_grid=qpg, start_cash=start_cash, **kw)
    variants = [
        ("陽春固定網格", dict(use_atr=False, use_regime=False, use_reset=False)),
        ("動態(ATR+重開)", dict(use_atr=True, use_regime=False, use_reset=True)),
        ("動態+ADX過濾", dict(use_atr=True, use_regime=True, use_reset=True)),
    ]
    out = []
    for label, opts in variants:
        bt = DynamicGridBacktester(**{**common, **opts})
        out.append(bt.run(klines, symbol, label=label))
    return out
