"""逐筆成交與網格現況匯出（給外部分析用）。

設計原則：**只給原始資料，不做任何配對或淨額計算**——配對邏輯由分析者
自己決定（FIFO / 加權平均 / 網格層對應各有適用場景，先幫人配好反而限制了分析）。

手續費的誠實說明：
`trades` 表從一開始就沒有 fee 欄位（疏漏），所以**歷史成交的手續費未記錄**。
匯出時 fee/fee_coin 留空並在 README 標明，不用 0 填充——0 會被誤讀成免手續費，
那會讓分析偏樂觀，正是要避免的事。權威來源是派網 App 自己的交易記錄匯出。
2026-10 起新成交會記錄實際手續費（見 store.record_trade 的 fee 參數）。
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TRADE_COLUMNS = [
    "trade_id", "time_utc", "time_taipei", "ts_epoch", "symbol", "side",
    "price", "base_qty", "quote_amount", "fee", "fee_coin",
    "source", "order_id", "simulated", "recorded_realized_pnl",
]


def _fmt_time(ts: float) -> tuple[str, str]:
    """(UTC, 台北時間) 兩種字串——分析者不必自己換算時區。"""
    utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (utc.strftime("%Y-%m-%d %H:%M:%S"),
            datetime.fromtimestamp(ts + 8 * 3600, tz=timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S"))


def trade_rows(rows: list[dict]) -> list[dict]:
    """把 DB 列轉成匯出列（逐筆，不配對、不聚合）。"""
    out = []
    for r in sorted(rows, key=lambda x: (x["ts"], x.get("id", 0))):
        utc, tpe = _fmt_time(float(r["ts"]))
        out.append({
            "trade_id": r.get("id", ""),
            "time_utc": utc,
            "time_taipei": tpe,
            "ts_epoch": f'{float(r["ts"]):.3f}',
            "symbol": r["symbol"],
            "side": str(r["side"]).upper(),
            "price": f'{float(r["price"]):.8f}',
            "base_qty": f'{float(r["base"]):.8f}',
            "quote_amount": f'{float(r["quote"]):.8f}',
            # 留空而非填 0——未記錄 ≠ 免費（見模組說明）
            "fee": r.get("fee") if r.get("fee") not in (None, "") else "",
            "fee_coin": r.get("fee_coin") or "",
            "source": r.get("source") or "",
            "order_id": r.get("order_id") or "",
            "simulated": 1 if r.get("simulated") else 0,
            "recorded_realized_pnl": f'{float(r.get("realized_pnl") or 0):.8f}',
        })
    return out


def open_positions(grid_state: Optional[dict],
                   current_price: float) -> dict:
    """目前未平倉的網格持倉：逐格數量與格線價 + 加權均價 + 未實現損益。

    網格的風險都藏在這裡——只看已實現損益會全是小賺。
    均價用「格線價」加權：網格買在格線上，實際成交價會有滑價，
    兩者差異已反映在 grid-report 的現金流數字裡（此處標示為估算）。
    """
    empty = {"active": False, "lots": [], "total_base": 0.0,
             "avg_cost": 0.0, "cost_value": 0.0,
             "market_value": 0.0, "unrealized": 0.0}
    if not grid_state or not grid_state.get("active"):
        return empty
    held = {int(k): float(v) for k, v in (grid_state.get("held") or {}).items()}
    if not held:
        return {**empty, "active": True}
    lower = float(grid_state["lower"])
    upper = float(grid_state["upper"])
    n = int(grid_state.get("grids", 10))
    step = (upper - lower) / n if n else 0.0

    lots = []
    total_base = cost_value = 0.0
    for idx in sorted(held):
        qty = held[idx]
        level_px = lower + idx * step
        lots.append({"grid_index": idx, "level_price": level_px, "base_qty": qty,
                     "cost_value": level_px * qty,
                     "market_value": current_price * qty,
                     "unrealized": (current_price - level_px) * qty})
        total_base += qty
        cost_value += level_px * qty
    market_value = total_base * current_price
    return {
        "active": True, "lots": lots, "total_base": total_base,
        "avg_cost": cost_value / total_base if total_base else 0.0,
        "cost_value": cost_value, "market_value": market_value,
        "unrealized": market_value - cost_value,
    }


def grid_meta(cfg, grid_state: Optional[dict],
              first_trade_ts: Optional[float]) -> dict:
    """網格參數：區間、格數、每格金額、配置資本、開始時間。"""
    g = cfg.raw.get("grid", {})
    grids = int(g.get("grids", 10))
    per = float(g.get("quote_per_grid", 5))
    meta = {
        "symbol": cfg.symbol,
        "mode": "live" if cfg.is_live else "paper",
        "grids": grids,
        "quote_per_grid": per,
        "allocated_capital": grids * per,
        "range_mode": g.get("range_mode", "fixed"),
        # range_pct 是「區間多寬」這個問題的答案，分析時一定要知道；
        # 只有 range_mode=fixed 時它才真的生效，所以兩個都匯出。
        "range_pct": g.get("range_pct", ""),
        "auto_range": g.get("auto_range", ""),
        "max_loss_quote": g.get("max_loss_quote", ""),
        "atr_mult": g.get("atr_mult", ""),
        "regime_filter": g.get("regime_filter", ""),
        "adx_max": g.get("adx_max", ""),
        "breakout_buffer": g.get("breakout_buffer", ""),
        "reset_on_breakout": g.get("reset_on_breakout", ""),
        "initial_capital_declared": cfg.trading.get("initial_capital", ""),
    }
    if grid_state and grid_state.get("active"):
        meta.update({
            "current_range_lower": f'{float(grid_state["lower"]):.2f}',
            "current_range_upper": f'{float(grid_state["upper"]):.2f}',
            "current_grid_created_price": grid_state.get("created_price", ""),
            "current_grid_active": 1,
        })
        # 開網格時間：回測重播要用它當起點。舊的狀態檔沒有這個欄位，留空。
        if grid_state.get("created_ts"):
            utc, tpe = _fmt_time(float(grid_state["created_ts"]))
            meta["current_grid_created_utc"] = utc
            meta["current_grid_created_taipei"] = tpe
    else:
        meta["current_grid_active"] = 0
    if first_trade_ts:
        utc, tpe = _fmt_time(first_trade_ts)
        meta["first_trade_utc"] = utc
        meta["first_trade_taipei"] = tpe
    return meta


def write_export(out_dir: str, *, rows: list[dict], cfg, grid_state,
                 current_price: float, fee_recorded_since: str = "") -> dict:
    """寫出 trades.csv / grid_meta.csv / open_positions.csv / README.txt。

    回傳摘要 dict 供 CLI 列印。"""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)

    trs = trade_rows(rows)
    with (d / "trades.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_COLUMNS)
        w.writeheader()
        w.writerows(trs)

    first_ts = float(rows[0]["ts"]) if rows else None
    if rows:
        first_ts = min(float(r["ts"]) for r in rows)
    meta = grid_meta(cfg, grid_state, first_ts)
    with (d / "grid_meta.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["key", "value"])
        for k, v in meta.items():
            w.writerow([k, v])

    pos = open_positions(grid_state, current_price)
    with (d / "open_positions.csv").open("w", newline="",
                                         encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["grid_index", "level_price", "base_qty", "cost_value",
                    "market_value", "unrealized"])
        for lot in pos["lots"]:
            w.writerow([lot["grid_index"], f'{lot["level_price"]:.2f}',
                        f'{lot["base_qty"]:.8f}', f'{lot["cost_value"]:.4f}',
                        f'{lot["market_value"]:.4f}', f'{lot["unrealized"]:.4f}'])
        w.writerow([])
        w.writerow(["TOTAL", f'avg_cost={pos["avg_cost"]:.2f}',
                    f'{pos["total_base"]:.8f}', f'{pos["cost_value"]:.4f}',
                    f'{pos["market_value"]:.4f}', f'{pos["unrealized"]:.4f}'])

    span = ""
    if rows:
        last_ts = max(float(r["ts"]) for r in rows)
        span = (f'{_fmt_time(first_ts)[0]} ~ {_fmt_time(last_ts)[0]} UTC'
                f'（{(last_ts - first_ts) / 86400:.1f} 天）')
    (d / "README.txt").write_text(_readme(len(trs), span, current_price, pos,
                                          fee_recorded_since),
                                  encoding="utf-8")
    return {"trades": len(trs), "span": span, "positions": pos, "dir": str(d)}


def _readme(n_trades: int, span: str, price: float, pos: dict,
            fee_recorded_since: str) -> str:
    fee_note = (f"自 {fee_recorded_since} 起的成交才有手續費記錄；在此之前的"
                "成交 fee/fee_coin 為空。"
                if fee_recorded_since else
                "**所有成交的 fee/fee_coin 皆為空——程式從未記錄手續費。**")
    return f"""派網網格機器人 — 逐筆成交匯出
================================

檔案
----
trades.csv          逐筆原始成交（未配對、未聚合、未淨額化）
grid_meta.csv       網格參數與本輪區間、開始時間
open_positions.csv  目前未平倉的逐格持倉、成本與未實現損益
README.txt          本說明

資料範圍
--------
成交筆數：{n_trades}
期間：{span or "（無成交）"}
匯出時現價：{price:.2f}

⚠ 手續費欄位（重要，不看會讓分析偏樂觀）
----------------------------------------
{fee_note}
請勿把空值當成 0——那會低估成本。
權威來源：派網 App/網頁 →「交易記錄」→ 匯出 CSV，內含每筆的實際手續費與幣別。
本匯出的成交價與數量來自交易所回報的實際成交（filled），可與派網匯出用
order_id 對照併表。

⚠ 未實現損益（網格的風險藏在這裡）
----------------------------------
目前未平倉 {pos['total_base']:.8f} 單位，加權均價 {pos['avg_cost']:.2f}，
成本 {pos['cost_value']:.2f}，市值 {pos['market_value']:.2f}，
未實現 {pos['unrealized']:+.2f}。
只看已實現損益會全部是小賺（網格只在獲利時才賣），虧損都留在未平倉部位裡。
分析請務必把這塊一起計入。

欄位說明（trades.csv）
----------------------
trade_id              資料庫流水號
time_utc / time_taipei 成交時間（兩種時區，同一時刻）
ts_epoch              Unix 時間戳（秒，便於程式處理）
symbol                交易對（BASE_QUOTE）
side                  BUY / SELL
price                 實際成交均價
base_qty              成交的基礎幣數量（如 BTC）
quote_amount          成交的報價幣金額（如 USDT），交易所回報值
fee / fee_coin        手續費與幣別（見上方說明）
source                來源：grid=網格收割、grid:close=跌破區間倒貨平倉、
                      strategy:*=策略訊號、manual:cli=手動
order_id              交易所訂單編號（與派網匯出對照用）
simulated             1=紙上模擬、0=實盤真錢
recorded_realized_pnl 機器人當下自記的已實現損益（網格用格線價估算、
                      不含手續費）——僅供參考，請以你自己的配對計算為準
"""
