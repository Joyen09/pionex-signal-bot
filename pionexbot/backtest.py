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
                 max_position_base: Optional[float] = None):
        self.strategy = strategy
        self.start_cash = start_cash
        self.quote_per_trade = quote_per_trade
        self.fee_rate = fee_rate
        self.max_position_base = max_position_base

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

        for i in range(2, n):
            window = klines[: i + 1]
            price = closes[i]
            signal = self.strategy.evaluate(window, symbol)

            if signal is not None:
                if signal.action == Action.BUY and cash >= self.quote_per_trade:
                    can_buy = True
                    if self.max_position_base is not None and base >= self.max_position_base:
                        can_buy = False
                    if can_buy:
                        spend = min(self.quote_per_trade, cash)
                        fee = spend * self.fee_rate
                        bought = (spend - fee) / price
                        total_cost = avg_cost * base + spend
                        base += bought
                        avg_cost = total_cost / base if base else 0.0
                        cash -= spend
                        open_trade = Trade(entry_price=price, entry_quote=spend,
                                           base=bought, reason_in=signal.reason)
                        trades.append(open_trade)
                elif signal.action in (Action.SELL, Action.CLOSE) and base > 0:
                    gross = base * price
                    proceeds = gross * (1 - self.fee_rate)
                    realized = (price - avg_cost) * base
                    cash += proceeds
                    if open_trade is not None:
                        open_trade.exit_price = price
                        open_trade.exit_quote = proceeds
                        open_trade.pnl = realized
                        open_trade.reason_out = signal.reason
                    base = 0.0
                    avg_cost = 0.0
                    open_trade = None

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
