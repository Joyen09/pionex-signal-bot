#!/usr/bin/env python3
"""派網訊號機器人 CLI。

用法：
    python main.py test            # 測試連線與 API 簽章（建議第一步）
    python main.py balance         # 查詢帳戶餘額（需 API 金鑰）
    python main.py price           # 查目前市價
    python main.py run-strategy    # 啟動內建策略機器人（輪詢 K 線）
    python main.py run-grid        # 啟動自動網格機器人（風險過高自動關閉重開）
    python main.py run-webhook     # 啟動 Webhook 伺服器（接 TradingView 等）
    python main.py status          # 顯示目前持倉與最近交易
    python main.py buy  --quote 20 # 手動下一筆市價買單
    python main.py sell --base 0.001  # 手動市價賣出
    python main.py backtest        # 用歷史 K 線回測策略
    python main.py backtest-sweep  # 掃描停損/停利組合，推薦最佳參數
    python main.py optimize        # walk-forward 最佳化（嚴謹驗證是否真有優勢）
    python main.py grid-backtest   # 網格交易回測（--lower --upper --grids）

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


def cmd_run_grid(bot: Bot) -> int:
    from pionexbot.sources.grid_runner import GridRunner
    runner = GridRunner(bot.cfg, bot.client, bot.broker, bot.store, bot.notifier)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        bot.notifier.send("🛑 網格機器人已手動停止。", "info", important=True)
    except Exception as exc:  # noqa: BLE001
        bot.notifier.send(f"💥 網格機器人異常結束：{exc}", "error")
        raise
    return 0


def cmd_run_dca(bot: Bot) -> int:
    from pionexbot.sources.dca_runner import DcaRunner
    runner = DcaRunner(bot.cfg, bot.client, bot.broker, bot.store, bot.notifier)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        bot.notifier.send("🛑 DCA 機器人已手動停止。", "info", important=True)
    except Exception as exc:  # noqa: BLE001
        bot.notifier.send(f"💥 DCA 機器人異常結束：{exc}", "error")
        raise
    return 0


def cmd_dca_backtest(bot: Bot, args) -> int:
    from pionexbot.dca import compare_dca

    cfg = bot.cfg
    symbol = (args.symbol or cfg.symbol).upper()
    interval = args.interval or "1D"
    limit = args.limit or 1000
    # every：每隔幾根 K 線買一次（1D 線時 every=7 ≈ 每週）
    every = args.grids or 7   # 借用 --grids 當 every，避免再加參數

    print(f"抓取 {symbol} {interval} 共 {limit} 根，比較 DCA（每 {every} 根買一次）...")
    try:
        klines = bot.client.get_klines_history(symbol, interval, total=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 50:
        print(f"❌ K 線太少（{len(klines)}）")
        return 1

    results = compare_dca(klines, base=10.0, every=every)
    print(f"\n{symbol} {interval}　{len(klines)} 根　每 {every} 根買 10 USDT")
    print("\n方式        投入    現值    報酬     均價    買入次數")
    print("─" * 56)
    for r in results:
        print(f"{r.label:<10}{r.invested:>6.0f}  {r.final_value:>6.0f}  "
              f"{r.total_return*100:+6.1f}%  {r.avg_cost:>8.0f}  {r.buys:>4}")
    print("\n判讀：『逢低加碼』的均價通常較低；在波動/下跌段報酬常優於陽春定額。")
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
        klines = bot.client.get_klines_history(cfg.symbol, interval, total=limit)
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
        klines = bot.client.get_klines_history(cfg.symbol, interval, total=limit)
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


def cmd_optimize(bot: Bot, args) -> int:
    from pionexbot.backtest import walk_forward

    cfg = bot.cfg
    scfg = cfg.strategy
    name = args.strategy or scfg.get("name", "ma_cross")
    interval = args.interval or scfg.get("interval", "4H")
    limit = args.limit or 5000

    print(f"抓取 {cfg.symbol} {interval} 共 {limit} 根 K 線，對 {name} 做 walk-forward 最佳化 ...")
    try:
        klines = bot.client.get_klines_history(cfg.symbol, interval, total=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 300:
        print(f"❌ K 線太少（{len(klines)}），無法做 walk-forward")
        return 1

    # 用「滿倉單一部位」評估，數字才反映策略真實複利績效
    cash = args.cash or 1000.0
    folds = walk_forward(name, klines, cfg.symbol, folds=4,
                         start_cash=cash, quote_per_trade=cash)
    if not folds:
        print("❌ 資料不足以切分，請加大 --limit")
        return 1

    print(f"\n策略={name}　K線={len(klines)}　（每段都『前段找參數→後段沒看過的資料驗收』）")
    print("\n段  最佳參數                              內測報酬   實測報酬   實測交易")
    print("─" * 78)
    compounded = 1.0
    test_returns = []
    for f in folds:
        compounded *= (1 + f.test_return)
        test_returns.append(f.test_return)
        ps = ",".join(f"{k}={v}" for k, v in f.params.items())
        print(f"{f.fold:>2}  {ps:<36}  {f.train_return*100:+7.2f}%  "
              f"{f.test_return*100:+7.2f}%  {f.test_trades:>6}")

    oos = (compounded - 1) * 100
    wins = sum(1 for r in test_returns if r > 0)
    print("─" * 78)
    print(f"\n📉 實測（out-of-sample）累積報酬：{oos:+.2f}%　"
          f"（{wins}/{len(test_returns)} 段為正）")
    print("⚠️  此為含手續費、但『不含滑價』的理想值；實盤每筆再扣約 0.1~0.3% 滑價。")

    if oos > 5 and wins >= len(test_returns) * 0.6:
        print("\n✅ 在沒看過的資料上多數為正且累積正報酬 → 較可能有真實優勢（仍需扣滑價評估）。")
    else:
        print("\n🔴 在沒看過的資料上未能穩定獲利 → 先前的高報酬多為過度擬合，不建議投真錢。")
    return 0


def cmd_grid_backtest(bot: Bot, args) -> int:
    from pionexbot.grid import GridBacktester, _ohlc

    cfg = bot.cfg
    symbol = (args.symbol or cfg.symbol).upper()
    interval = args.interval or "4H"
    limit = args.limit or 2000

    print(f"抓取 {symbol} {interval} 共 {limit} 根 K 線 ...")
    try:
        klines = bot.client.get_klines_history(symbol, interval, total=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 50:
        print(f"❌ K 線太少（{len(klines)}）")
        return 1

    first_close = _ohlc(klines[0])[2]
    # 未指定區間時，預設用起始價 ±20% 當網格範圍（事前可知的合理選擇）
    lower = args.lower or first_close * 0.8
    upper = args.upper or first_close * 1.2
    grids = args.grids or 20
    quote = args.quote or 20.0

    print(f"網格區間 {lower:.2f} ~ {upper:.2f}，{grids} 格，每格 {quote} USDT")
    bt = GridBacktester(lower, upper, grids, quote_per_grid=quote)
    result = bt.run(klines, symbol)
    print(result.summary())
    if result.unrealized < -result.start_cash * 0.1:
        print("\n⚠️ 未實現虧損偏大：價格可能已跌破網格下緣（住套房），這是網格在跌勢的典型風險。")
    return 0


def cmd_grid_compare(bot: Bot, args) -> int:
    from pionexbot.grid import compare_grid_variants

    cfg = bot.cfg
    symbol = (args.symbol or cfg.symbol).upper()
    interval = args.interval or "4H"
    limit = args.limit or 3000
    grids = args.grids or 20
    cash = args.cash or 1000.0

    print(f"抓取 {symbol} {interval} 共 {limit} 根 K 線，比較三種網格 ...")
    try:
        klines = bot.client.get_klines_history(symbol, interval, total=limit)
    except PionexError as exc:
        print(f"❌ 抓 K 線失敗：{exc}")
        return 1
    if len(klines) < 200:
        print(f"❌ K 線太少（{len(klines)}）")
        return 1

    results = compare_grid_variants(klines, symbol, grids=grids, start_cash=cash)
    bh = results[0].buy_hold_return * 100
    print(f"\n資料 {len(klines)} 根　起始資金 {cash:.0f}　{grids} 格　"
          f"買入持有 {bh:+.2f}%")
    print("\n策略              報酬      最大回撤  完成來回  重開  暫停%")
    print("─" * 62)
    for r in results:
        paused_pct = r.paused_bars / r.bars * 100 if r.bars else 0
        print(f"{r.label:<16}{r.total_return*100:+7.2f}%  {r.max_drawdown*100:6.1f}%  "
              f"{r.completed:>7}  {r.resets:>4}  {paused_pct:4.0f}%")
    print("\n判讀：比較『動態+ADX過濾』vs『陽春固定』的報酬與回撤；"
          "若前者報酬更高或回撤更小，代表 ATR/ADX 升級有效。")
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
                                 "run-strategy", "run-grid", "run-dca", "run-webhook",
                                 "buy", "sell",
                                 "backtest", "backtest-sweep", "optimize",
                                 "grid-backtest", "grid-compare",
                                 "dca-backtest", "notify-test"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--quote", type=float, help="買入金額（報價幣）")
    parser.add_argument("--base", type=float, help="賣出數量（基礎幣）")
    parser.add_argument("--strategy", help="回測指定策略")
    parser.add_argument("--interval", help="K 線週期")
    parser.add_argument("--limit", type=int, help="回測 K 線根數")
    parser.add_argument("--cash", type=float, help="回測起始資金")
    parser.add_argument("--lower", type=float, help="網格下緣價格")
    parser.add_argument("--upper", type=float, help="網格上緣價格")
    parser.add_argument("--grids", type=int, help="網格數量")
    parser.add_argument("--symbol", help="覆寫交易對（如 ETH_USDT），用於回測掃描")
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
        if args.command == "run-grid":
            return cmd_run_grid(bot)
        if args.command == "run-dca":
            return cmd_run_dca(bot)
        if args.command == "run-webhook":
            return cmd_run_webhook(bot)
        if args.command == "backtest":
            return cmd_backtest(bot, args)
        if args.command == "backtest-sweep":
            return cmd_backtest_sweep(bot, args)
        if args.command == "optimize":
            return cmd_optimize(bot, args)
        if args.command == "grid-backtest":
            return cmd_grid_backtest(bot, args)
        if args.command == "grid-compare":
            return cmd_grid_compare(bot, args)
        if args.command == "dca-backtest":
            return cmd_dca_backtest(bot, args)
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
