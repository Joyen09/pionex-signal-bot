"""以 SQLite 記錄交易與機器人狀態（持倉、當日已實現損益）。"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Position:
    base: float = 0.0          # 目前持有的基礎幣數量
    avg_cost: float = 0.0      # 平均成本價
    realized_pnl_today: float = 0.0
    day: str = ""              # YYYY-MM-DD（用來判斷是否換日重置）
    last_trade_ts: float = 0.0


class Store:
    def __init__(self, path: str = "data/bot.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                base REAL NOT NULL,
                quote REAL NOT NULL,
                price REAL NOT NULL,
                simulated INTEGER NOT NULL,
                source TEXT,
                order_id TEXT,
                realized_pnl REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    # --- 狀態鍵值 ---
    def _get(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def load_position(self) -> Position:
        return Position(
            base=float(self._get("pos_base", "0") or 0),
            avg_cost=float(self._get("pos_avg_cost", "0") or 0),
            realized_pnl_today=float(self._get("pnl_today", "0") or 0),
            day=self._get("pnl_day", ""),
            last_trade_ts=float(self._get("last_trade_ts", "0") or 0),
        )

    def save_position(self, pos: Position) -> None:
        self._set("pos_base", repr(pos.base))
        self._set("pos_avg_cost", repr(pos.avg_cost))
        self._set("pnl_today", repr(pos.realized_pnl_today))
        self._set("pnl_day", pos.day)
        self._set("last_trade_ts", repr(pos.last_trade_ts))

    def record_trade(self, *, symbol: str, side: str, base: float, quote: float,
                     price: float, simulated: bool, source: str = "",
                     order_id: str = "", realized_pnl: float = 0.0) -> None:
        self.conn.execute(
            "INSERT INTO trades(ts,symbol,side,base,quote,price,simulated,source,order_id,realized_pnl)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (time.time(), symbol, side, base, quote, price,
             1 if simulated else 0, source, order_id, realized_pnl),
        )
        self.conn.commit()

    def recent_trades(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def set_meta(self, key: str, value) -> None:
        self._set(key, str(value))

    def get_meta(self, key: str, default=None):
        v = self._get(key, "")
        return v if v != "" else default

    def save_grid_state(self, state: dict) -> None:
        self._set("grid_state", json.dumps(state))

    def load_grid_state(self) -> Optional[dict]:
        raw = self._get("grid_state", "")
        return json.loads(raw) if raw else None

    def save_dca_state(self, state: dict) -> None:
        self._set("dca_state", json.dumps(state))

    def load_dca_state(self) -> Optional[dict]:
        raw = self._get("dca_state", "")
        return json.loads(raw) if raw else None

    # --- 交易計畫（SL / 多段 TP）---
    def save_plan(self, plan: dict) -> None:
        """儲存目前部位的出場計畫（entry/sl/tps/初始數量等）。"""
        self._set("trade_plan", json.dumps(plan))

    def load_plan(self) -> Optional[dict]:
        raw = self._get("trade_plan", "")
        return json.loads(raw) if raw else None

    def clear_plan(self) -> None:
        self._set("trade_plan", "")

    def count_buys_since(self, ts: float) -> int:
        """自 ts(秒) 以來的「訊號類」買入筆數，供每日開單上限使用。

        排除網格 / DCA 的例行買入——它們有自己的節奏控制，且與策略共用同一個
        資料庫；若把它們算進來，網格買幾格就會誤擋策略進場。"""
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM trades WHERE ts >= ? AND side = 'BUY' "
            "AND source NOT LIKE 'grid%' AND source NOT LIKE 'dca%'", (ts,)
        ).fetchone()
        return int(row["c"])

    def stats_since(self, ts: float) -> tuple[int, float]:
        """回傳自 ts(秒) 以來的 (成交筆數, 已實現損益總和)。"""
        row = self.conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(realized_pnl), 0) AS p "
            "FROM trades WHERE ts >= ?", (ts,)
        ).fetchone()
        return int(row["c"]), float(row["p"])

    def close(self) -> None:
        self.conn.close()
