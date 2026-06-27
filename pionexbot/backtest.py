"""回測引擎：用歷史 K 線重播策略，評估績效。

模擬規則（貼近現貨紙上交易）：
- 收到 BUY 且尚未滿倉：用 quote_per_trade 的報價幣在當根收盤價買入（扣手續費）
- 收到 SELL/CLOSE 且有持倉：以當根收盤價全部賣出（扣手續費）
- 不使用槓桿、不做空

輸出：總報酬率、買入持有報酬率、交易次數、勝率、最大回撤、平均每筆損益。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Action
from .strategy import build_strategy
from .strategy.base import Strategy


@dataclass
class Trade:
    entry_price: float
    entry_quote: float
    base: float
    exit_price: float = 0.0
    exit_quote: float = 0.0
    pnl: float = 0.0
    reason_in: str = ""
    reason_out: str = ""


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    bars: int
    start_equity: float
    end_equity: float
    trades: list[Trade] = field(default_factory=list)
    buy_hold_return: float = 0.0
    max_drawdown: float = 0.0

    @property
    def total_return(self) -> float:
        if self.start_equity == 0:
            return 0.0
        return (self.end_equity - self.start_equity) / self.start_equity

    @property
    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.exit_price > 0]

    @property
    def win_rate(self) -> float:
        closed = self.closed_trades
        if not closed:
            return 0.0
        wins = sum(1 for t in closed if t.pnl > 0)
        return wins / len(closed)

    @property
    def avg_pnl(self) -> float:
        closed = self.closed_trades
        return sum(t.pnl for t in closed) / len(closed) if closed else 0.0

    def summary(self) -> str:
        lines = [
            f"════════ 回測結果 ════════",
            f"  交易對       : {self.symbol}",
            f"  策略         : {self.strategy}",
            f"  K 線根數     : {self.bars}",
            f"  起始資金     : {self.start_equity:.2f}",
            f"  期末權益     : {self.end_equity:.2f}",
            f"  策略總報酬   : {self.total_return * 100:+.2f}%",
            f"  買入持有報酬 : {self.buy_hold_return * 100:+.2f}%",
            f"  最大回撤     : {self.max_drawdown * 100:.2f}%",
            f"  完成交易次數 : {len(self.closed_trades)}",
            f"  勝率         : {self.win_rate * 100:.1f}%",
            f"  平均每筆損益 : {self.avg_pnl:+.2f}",
            f"══════════════════════════",
        ]
        return "\n".join(lines)


class Backtester:
    def __init__(self, strategy: Strategy, *, start_cash: float = 1000.0,
                 quote_per_trade: float = 100.0, fee_rate: float = 0.0005,
                 max_position_base: Optional[float] = None,
                 stop_loss_pct: float = 0.0, take_profit_pct: float = 0.0):
        self.strategy = strategy
        self.start_cash = start_cash
        self.quote_per_trade = quote_per_trade
        self.fee_rate = fee_rate
        self.max_position_base = max_position_base
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct

    def run(self, klines: list[dict[str, Any]], symbol: str) -> BacktestResult:
        closes = Strategy.closes(klines)
        n = len(closes)
        cash = self.start_cash
        base = 0.0
        avg_cost = 0.0
        trades: list[Trade] = []
        open_trade: Optional[Trade] = None
        equity_curve: list[float] = []
        peak = self.start_cash
        max_dd = 0.0

        def close_position(price: float, reason: str) -> None:
            nonlocal cash, base, avg_cost, open_trade
            proceeds = base * price * (1 - self.fee_rate)
            realized = (price - avg_cost) * base
            cash += proceeds
            if open_trade is not None:
                open_trade.exit_price = price
                open_trade.exit_quote = proceeds
                open_trade.pnl = realized
                open_trade.reason_out = reason
            base = 0.0
            avg_cost = 0.0
            open_trade = None

        for i in range(2, n):
            price = closes[i]

            # 1) 持倉時先檢查停損 / 停利（模擬 live 每輪檢查）
            if base > 0 and avg_cost > 0 and (self.stop_loss_pct or self.take_profit_pct):
                change = (price - avg_cost) / avg_cost
                if self.take_profit_pct and change >= self.take_profit_pct:
                    close_position(price, f"停利 +{change * 100:.1f}%")
                elif self.stop_loss_pct and change <= -self.stop_loss_pct:
                    close_position(price, f"停損 {change * 100:.1f}%")

            # 2) 策略訊號（單一部位：空手才買、有倉才賣，與 live 一致）
            signal = self.strategy.evaluate(klines[: i + 1], symbol)
            if signal is not None:
                if signal.action == Action.BUY and base == 0 and cash >= self.quote_per_trade:
                    spend = min(self.quote_per_trade, cash)
                    fee = spend * self.fee_rate
                    bought = (spend - fee) / price
                    base = bought
                    avg_cost = spend / bought if bought else 0.0
                    cash -= spend
                    open_trade = Trade(entry_price=price, entry_quote=spend,
                                       base=bought, reason_in=signal.reason)
                    trades.append(open_trade)
                elif signal.action in (Action.SELL, Action.CLOSE) and base > 0:
                    close_position(price, signal.reason)

            equity = cash + base * price
            equity_curve.append(equity)
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak)

        # 期末以最後價格結算（未平倉部位以市價計入權益，不強制平倉）
        last_price = closes[-1]
        end_equity = cash + base * last_price
        buy_hold = (closes[-1] - closes[2]) / closes[2] if closes[2] else 0.0

        return BacktestResult(
            symbol=symbol, strategy=self.strategy.name, bars=n,
            start_equity=self.start_cash, end_equity=end_equity,
            trades=trades, buy_hold_return=buy_hold, max_drawdown=max_dd,
        )


def run_backtest(strategy_name: str, params: dict, klines: list[dict[str, Any]],
                 symbol: str, **kwargs) -> BacktestResult:
    strategy = build_strategy(strategy_name, params)
    return Backtester(strategy, **kwargs).run(klines, symbol)


# 停損 / 停利掃描用的預設網格（0 = 不啟用）
DEFAULT_SL_GRID = [0.0, 0.02, 0.03, 0.05, 0.08]
DEFAULT_TP_GRID = [0.0, 0.04, 0.06, 0.10, 0.15]


@dataclass
class SweepRow:
    stop_loss: float
    take_profit: float
    result: BacktestResult

    @property
    def score(self) -> float:
        """風險調整分數 = 報酬 / 最大回撤（回撤越小、報酬越高越好）。"""
        return self.result.total_return / max(self.result.max_drawdown, 0.01)


def sweep_stop_params(strategy_name: str, params: dict,
                      klines: list[dict[str, Any]], symbol: str,
                      sl_grid: Optional[list[float]] = None,
                      tp_grid: Optional[list[float]] = None,
                      **kwargs) -> list[SweepRow]:
    """對 (停損, 停利) 網格逐一回測，回傳所有結果。"""
    sl_grid = sl_grid if sl_grid is not None else DEFAULT_SL_GRID
    tp_grid = tp_grid if tp_grid is not None else DEFAULT_TP_GRID
    rows: list[SweepRow] = []
    for sl in sl_grid:
        for tp in tp_grid:
            strategy = build_strategy(strategy_name, params)
            r = Backtester(strategy, stop_loss_pct=sl, take_profit_pct=tp,
                           **kwargs).run(klines, symbol)
            rows.append(SweepRow(sl, tp, r))
    return rows
