#!/usr/bin/env python3
"""派網訊號機器人 CLI。

用法：
    python main.py test            # 測試連線與 API 簽章（建議第一步）
    python main.py balance         # 查詢帳戶餘額（需 API 金鑰）
    python main.py price           # 查目前市價
    python main.py run-strategy    # 啟動內建策略機器人（輪詢 K 線）
    python main.py run-webhook     # 啟動 Webhook 伺服器（接 TradingView 等）
    python main.py status          # 顯示目前持倉與最近交易
    python main.py buy  --quote 20 # 手動下一筆市價買單
    python main.py sell --base 0.001  # 手動市價賣出
    python main.py backtest        # 用歷史 K 線回測策略
    python main.py backtest-sweep  # 掃描停損/停利組合，推薦最佳參數

回測參數：
    --strategy ma_cross   指定策略（預設讀 config）
    --interval 5M         K 線週期
    --limit 1000          回測用的 K 線根數
    --cash 1000           起始資金

共用參數：
    --config config.yaml   指定設定檔
"""
from __future__ import annotations

import argparse
import sys

from pionexbot.bot import Bot
from pionexbot.config import load_config
from pionexbot.models import Action, Signal
from pionexbot.pionex_client import PionexError


def cmd_test(bot: Bot) -> int:
    cfg = bot.cfg
    print(f"模式：{cfg.mode}　交易對：{cfg.symbol}　Base URL：{cfg.base_url}")
    problems = cfg.validate()
    if problems:
        print("⚠ 設定問題：")
        for p in problems:
            print("  -", p)

    # 1) 公開行情（不需金鑰）
    print("\n[1] 測試公開行情 ...")
    try:
        price = bot.client.get_ticker_price(cfg.symbol)
        print(f"  ✅ {cfg.symbol} 市價 = {price}")
    except PionexError as exc:
        print(f"  ❌ 行情失敗：{exc}")
        return 1

    # 2) 私有端點（驗證簽章）
    print("\n[2] 測試私有端點 / API 簽章 ...")
    if not cfg.secrets.has_api_keys:
        print("  ⏭ 未設定 API 金鑰，略過。（紙上交易仍可運作）")
        return 0
    try:
        balances = bot.client.get_balances()
        nonzero = {k: v for k, v in balances.items() if v > 0}
        print(f"  ✅ 簽章正確，餘額：{nonzero or '（皆為 0）'}")
    except PionexError as exc:
        print(f"  ❌ 私有端點失敗（簽章或權限問題）：{exc}")
        return 1
    print("\n全部通過 ✅")
    return 0


def cmd_balance(bot: Bot) -> int:
    try:
        balances = bot.client.get_balances()
    except PionexError as exc:
        print(f"❌ {exc}")
        return 1
    for coin, free in sorted(balances.items()):
        if free > 0:
            print(f"  {coin:>8}: {free}")
    return 0


def cmd_price(bot: Bot) -> int:
    try:
        print(f"{bot.cfg.symbol} = {bot.client.get_ticker_price(bot.cfg.symbol)}")
    except PionexError as exc:
        print(f"❌ {exc}")
        return 1
    return 0


def cmd_status(bot: Bot) -> int:
    pos = bot.store.load_position()
    print(f"持倉：{pos.base} @ 均價 {pos.avg_cost:.2f}")
    print(f"當日已實現損益（{pos.day or 'N/A'}）：{pos.realized_pnl_today:.2f}")
    print("\n最近交易：")
    rows = bot.store.recent_trades(10)
    if not rows:
        print("  （無）")
    for r in rows:
        tag = "模擬" if r["simulated"] else "實盤"
        print(f"  [{tag}] {r['side']:>4} {r['base']:.8f} @ {r['price']:.2f} "
              f"= {r['quote']:.2f}　pnl={r['realized_pnl']:.2f}　{r['source']}")
    return 0


def cmd_run_strategy(bot: Bot) -> int:
    from pionexbot.sources.strategy_runner import StrategyRunner
    runner = StrategyRunner(bot.cfg, bot.client, bot.executor, bot.notifier)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        bot.notifier.send("🛑 策略機器人已手動停止。", "info", important=True)
    except Exception as exc:  # noqa: BLE001 - 意外崩潰也要通知
        bot.notifier.send(f"💥 策略機器人異常結束：{exc}", "error")
        raise
    return 0


def cmd_run_webhook(bot: Bot) -> int:
    import uvicorn
    from pionexbot.sources.webhook import build_app
    app = build_app(bot.cfg, bot.executor, bot.notifier)
    wh = bot.cfg.webhook
    mode = "實盤" if bot.cfg.is_live else "紙上"
    print(f"🤖 Webhook 伺服器啟動（{mode}模式）"
          f" http://{wh.get('host','0.0.0.0')}:{wh.get('port',8080)}{wh.get('path','/webhook')}")
    uvicorn.run(app, host=wh.get("host", "0.0.0.0"), port=int(wh.get("port", 8080)))
    return 0


def cmd_backtest(bot: Bot, args) -> int:
    from pionexbot.backtest import Backtester
    from pionexbot.strategy import build_strategy

    cfg = bot.cfg
    scfg = cfg.strategy
    name = args.strategy or scfg.get("name", "ma_cross")
    interval = args.interval or scfg.get("interval", "5M")
    limit = args.limit or 1000

    print(f"抓取 {cfg.symbol} {interval} 共 {limit} 根 K 線 ...")
    try:
        klines = bot.client.get_klines(cfg.symbol, interval, limit=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 30:
        print(f"❌ K 線資料太少（{len(klines)} 根），無法回測")
        return 1

    strategy = build_strategy(name, scfg.get("params", {}))
    bt = Backtester(
        strategy,
        start_cash=args.cash or 1000.0,
        quote_per_trade=float(cfg.trading.get("quote_per_trade", 100)),
        max_position_base=float(cfg.risk.get("max_position_base", 0)) or None,
    )
    result = bt.run(klines, cfg.symbol)
    print(result.summary())
    return 0


def cmd_backtest_sweep(bot: Bot, args) -> int:
    from pionexbot.backtest import sweep_stop_params

    cfg = bot.cfg
    scfg = cfg.strategy
    name = args.strategy or scfg.get("name", "ma_cross")
    interval = args.interval or scfg.get("interval", "5M")
    limit = args.limit or 1000

    print(f"抓取 {cfg.symbol} {interval} 共 {limit} 根 K 線，掃描停損/停利組合 ...")
    try:
        klines = bot.client.get_klines(cfg.symbol, interval, limit=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 50:
        print(f"❌ K 線太少（{len(klines)}），無法回測")
        return 1

    rows = sweep_stop_params(
        name, scfg.get("params", {}), klines, cfg.symbol,
        start_cash=args.cash or 1000.0,
        quote_per_trade=float(cfg.trading.get("quote_per_trade", 100)),
    )

    # 依報酬排序印表
    by_return = sorted(rows, key=lambda r: r.result.total_return, reverse=True)
    print(f"\n策略={name}  K線={len(klines)}  "
          f"買入持有報酬={by_return[0].result.buy_hold_return * 100:+.2f}%")
    print("\n停損   停利   報酬      回撤    交易  勝率")
    print("─" * 48)
    for r in by_return:
        sl = f"{r.stop_loss*100:.0f}%" if r.stop_loss else "—"
        tp = f"{r.take_profit*100:.0f}%" if r.take_profit else "—"
        res = r.result
        print(f"{sl:>4}  {tp:>4}  {res.total_return*100:+7.2f}%  "
              f"{res.max_drawdown*100:5.1f}%  {len(res.closed_trades):4}  "
              f"{res.win_rate*100:4.0f}%")

    best_return = by_return[0]
    best_risk = max(rows, key=lambda r: r.score)
    print("\n🏆 報酬最高：停損 {} / 停利 {} → 報酬 {:+.2f}%、回撤 {:.1f}%".format(
        f"{best_return.stop_loss*100:.0f}%" if best_return.stop_loss else "關閉",
        f"{best_return.take_profit*100:.0f}%" if best_return.take_profit else "關閉",
        best_return.result.total_return * 100, best_return.result.max_drawdown * 100))
    print("⚖️  風險調整最佳（報酬/回撤）：停損 {} / 停利 {} → 報酬 {:+.2f}%、回撤 {:.1f}%".format(
        f"{best_risk.stop_loss*100:.0f}%" if best_risk.stop_loss else "關閉",
        f"{best_risk.take_profit*100:.0f}%" if best_risk.take_profit else "關閉",
        best_risk.result.total_return * 100, best_risk.result.max_drawdown * 100))
    print("\n建議把 config.yaml 的 risk 改成（風險調整最佳那組）：")
    print(f"  stop_loss_pct: {best_risk.stop_loss}")
    print(f"  take_profit_pct: {best_risk.take_profit}")
    return 0


def cmd_notify_test(bot: Bot) -> int:
    n = bot.notifier
    print(f"Telegram 啟用：{n.tg_enabled}　LINE 啟用：{n.line_enabled}")
    if not (n.tg_enabled or n.line_enabled):
        print("⚠ 兩種通知都未啟用。請在 config.yaml 設 enabled: true，並在 .env 填好金鑰。")
        return 1
    n.send("🔔 派網訊號機器人通知測試：如果你收到這則訊息，代表通知設定成功！",
           "info", important=True)
    print("已送出測試通知，請查看你的 LINE / Telegram。")
    return 0


def cmd_manual(bot: Bot, action: Action, quote: float | None, base: float | None) -> int:
    sig = Signal(action=action, symbol=bot.cfg.symbol, source="manual:cli",
                 quote_amount=quote, base_size=base, reason="手動下單")
    result = bot.executor.handle(sig)
    print(result or "未成交（被風控擋下或 HOLD）")
    return 0 if (result and result.ok) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="派網訊號機器人")
    parser.add_argument("command",
                        choices=["test", "balance", "price", "status",
                                 "run-strategy", "run-webhook", "buy", "sell",
                                 "backtest", "backtest-sweep", "notify-test"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--quote", type=float, help="買入金額（報價幣）")
    parser.add_argument("--base", type=float, help="賣出數量（基礎幣）")
    parser.add_argument("--strategy", help="回測指定策略")
    parser.add_argument("--interval", help="K 線週期")
    parser.add_argument("--limit", type=int, help="回測 K 線根數")
    parser.add_argument("--cash", type=float, help="回測起始資金")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    bot = Bot(cfg)
    try:
        if args.command == "test":
            return cmd_test(bot)
        if args.command == "balance":
            return cmd_balance(bot)
        if args.command == "price":
            return cmd_price(bot)
        if args.command == "status":
            return cmd_status(bot)
        if args.command == "run-strategy":
            return cmd_run_strategy(bot)
        if args.command == "run-webhook":
            return cmd_run_webhook(bot)
        if args.command == "backtest":
            return cmd_backtest(bot, args)
        if args.command == "backtest-sweep":
            return cmd_backtest_sweep(bot, args)
        if args.command == "notify-test":
            return cmd_notify_test(bot)
        if args.command == "buy":
            return cmd_manual(bot, Action.BUY, args.quote or
                              float(cfg.trading.get("quote_per_trade", 20)), None)
        if args.command == "sell":
            return cmd_manual(bot, Action.SELL, None, args.base)
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
