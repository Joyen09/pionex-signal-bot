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
                                 "run-strategy", "run-webhook", "buy", "sell"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--quote", type=float, help="買入金額（報價幣）")
    parser.add_argument("--base", type=float, help="賣出數量（基礎幣）")
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
