"""ict2022 策略執行器（live / paper 共用）。

流程：每輪輪詢 →
  1. 有部位 → 交給 Phase 1 的出場計畫（executor.check_plan）管 SL/TP/保本
  2. 有掛單 → 檢查失效（逾期 / 未回踩即創新高 / 反向 MSS）、觸價則市價進場
     （進場訊號自帶 SL/多段 TP/tags，倉位由風控以止損距離計算）
  3. 空手無掛單 → 新 trigger K 收盤時跑 find_setup 找 setup

與回測共用 find_setup 與 SMC 偵測（規格 §1）。做多限定（現貨）。
"""
from __future__ import annotations

import time
from typing import Optional

from ..config import Config
from ..models import Action, Signal
from ..notifier import Notifier
from ..smc import ohlc, structure
from ..smc.mtf import interval_ms
from ..smc.sessions import SessionClock
from ..smc.types import Direction, StructureKind
from ..strategy.ict2022 import DEFAULTS, Setup, find_setup

TRIGGER_WINDOW = 300
HTF_WINDOW = 200


class IctRunner:
    def __init__(self, cfg: Config, client, executor, notifier: Notifier):
        self.cfg = cfg
        self.client = client
        self.executor = executor
        self.notifier = notifier

        self.p = {**DEFAULTS, **cfg.strategy.get("ict2022", {})}
        self.smc_cfg = cfg.raw.get("smc", {})
        self.clock = SessionClock(self.smc_cfg.get("sessions"))
        self.symbol = cfg.symbol
        self.poll_seconds = int(cfg.strategy.get("poll_seconds", 30))

        self._running = False
        self._pending: Optional[Setup] = None
        self._pending_expire_ts = 0.0
        self._pending_trig_time = None
        self._dead: set = set()
        self._last_trig_time = None
        self._last_entry_time = None

    # ----- 資料 -----
    def _closed(self, interval: str, limit: int) -> list:
        kl = self.client.get_klines(self.symbol, interval, limit=limit + 1)
        return kl[:-1] if len(kl) >= 2 else []

    # ----- 主邏輯 -----
    def run_once(self) -> None:
        entry_int = str(self.p["entry_interval"])
        entry = self._closed(entry_int, 5)
        trig = self._closed(str(self.p["trigger_interval"]), TRIGGER_WINDOW)
        htf = self._closed(str(self.p["htf_interval"]), HTF_WINDOW)
        if not entry or len(trig) < 30 or len(htf) < 20:
            return
        price = self.executor.broker.get_price(self.symbol)
        if not price or price <= 0:
            return

        new_entry_candle = ohlc.t(entry[-1]) != self._last_entry_time
        if new_entry_candle:
            self._last_entry_time = ohlc.t(entry[-1])
        new_trig_candle = ohlc.t(trig[-1]) != self._last_trig_time
        if new_trig_candle:
            self._last_trig_time = ohlc.t(trig[-1])

        # 1) 有部位 → 出場計畫（TP 觸價每輪、SL 於新 entry 收盤）
        if self.executor.store.load_position().base > 0:
            self._pending = None   # 進場後不再留掛單（單一部位）
            self.executor.check_plan(
                price,
                candle_close=ohlc.c(entry[-1]) if new_entry_candle else None)
            return

        # 2) 掛單管理
        if self._pending is not None:
            s = self._pending
            cancel = ""
            if ohlc.c(entry[-1]) > s.range_high:
                cancel = "未回踩即創新高"
            elif time.time() >= self._pending_expire_ts:
                cancel = "掛單逾期"
            elif new_trig_candle:
                st = structure.detect_structure(trig)
                if any(e.kind == StructureKind.MSS
                       and e.direction == Direction.DOWN
                       and e.confirmed_at >= len(trig) - 2
                       for e in st.events):
                    cancel = "反向 MSS"
            if cancel:
                self._dead.add(s.key)
                self._pending = None
                self.notifier.send(f"🗑 撤銷 ict2022 掛單（{cancel}）", "info",
                                   important=True)
            elif price <= s.limit_price:
                self._fill(s, price)
            return

        # 3) 找新 setup（新 trigger K 收盤才重算）
        if not new_trig_candle:
            return
        kz = self.clock.in_killzone(time.time() * 1000)
        s = find_setup(htf, trig, self.p, self.smc_cfg, killzone=kz)
        if s is None or s.key in self._dead:
            return
        self._pending = s
        self._pending_expire_ts = time.time() + (
            int(s.expiry_bars) * interval_ms(entry_int) / 1000)
        self._pending_trig_time = ohlc.t(trig[-1])
        tp_txt = " / ".join(f"{t.price:.2f}×{t.fraction}" for t in s.take_profits)
        self.notifier.send(
            f"📌 ict2022 掛單：限價 {s.limit_price:.2f}｜SL {s.stop_loss:.2f}"
            f"｜TP {tp_txt}｜RR(TP1)={s.tags.get('rr_tp1')}"
            f"｜{s.tags.get('zone_kind')}{'·OTE' if s.tags.get('in_ote') else ''}",
            "info", important=True)

    def _fill(self, s: Setup, price: float) -> None:
        """觸價 → 市價進場（v1 簡化：與 TP 同為 touch 成交模型）。"""
        self._dead.add(s.key)
        self._pending = None
        sig = Signal(
            Action.BUY, self.symbol, source="strategy:ict2022",
            reason=f"ict2022 觸價 {s.limit_price:.2f}（{s.tags.get('zone_kind')}）",
            stop_loss=s.stop_loss, take_profits=s.take_profits, tags=s.tags)
        self.executor.handle(sig)

    def run_forever(self) -> None:
        self._running = True
        mode = "實盤" if self.cfg.is_live else "紙上"
        self.notifier.send(
            f"🤖 ict2022 機器人啟動（{mode}模式）｜{self.symbol}｜"
            f"HTF {self.p['htf_interval']} / trigger {self.p['trigger_interval']}"
            f" / entry {self.p['entry_interval']}", "info", important=True)
        while self._running:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                self.notifier.send(f"⚠️ ict2022 迴圈錯誤：{exc}", "error")
            time.sleep(self.poll_seconds)

    def stop(self) -> None:
        self._running = False
