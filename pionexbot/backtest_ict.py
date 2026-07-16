"""ict2022 專用回測：限價進場、SL/多段 TP、保本、R 倍數統計（規格 §5.2）。

成交模型與實盤（§2.3）一致：
- 限價進場：entry 時框 low 觸及掛單價 → 以掛單價成交（touch-based）
- TP：high 觸及 → 以 TP 價成交（限價）
- SL：收盤觸發（close <= SL → 以收盤價 × (1−slippage) 市價出）
- 同一根同時觸 TP 與 SL → 以 SL 計（fill_ambiguity: worst）
- 進場當根收盤即低於 SL → 視為立即止損（−1R）
- TP1 成交後 SL 移保本（entry × (1+offset)）

防 lookahead：trigger / HTF 序列用 MtfView 切片，entry 第 i 根收盤時
只看得到「已收盤」的高階 K 線。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .smc import ohlc, structure
from .smc.mtf import MtfView, close_time
from .smc.sessions import SessionClock
from .smc.types import Direction, StructureKind
from .strategy.ict2022 import DEFAULTS, Setup, find_setup

TRIGGER_WINDOW = 300   # setup 評估用的 trigger 視窗（sweep lookback 才 60，足夠）
HTF_WINDOW = 200


@dataclass
class IctTrade:
    entry: float
    stop_loss: float
    qty: float
    risk_quote: float
    opened_at: int
    tags: dict
    pnl: float = 0.0
    closed: bool = False
    closed_at: Optional[int] = None
    exit_reason: str = ""
    # 成交模型診斷（止損家族出場才填）：參考止損價 vs 實際成交價、觸發根 OHLC
    sl_ref: Optional[float] = None
    exit_px: Optional[float] = None
    trigger_ohlc: Optional[tuple] = None

    @property
    def r(self) -> float:
        return self.pnl / self.risk_quote if self.risk_quote else 0.0

    @property
    def sl_slip_r(self) -> Optional[float]:
        """止損成交價低於止損價的「超越量」，以原始 R 為單位。
        觸價模型下應 ≈0（僅剩 slippage_pct 與 gap）；收盤模型下會系統性為正。"""
        if self.sl_ref is None or self.exit_px is None:
            return None
        risk_dist = self.entry - self.stop_loss
        if risk_dist <= 0:
            return None
        return (self.sl_ref - self.exit_px) / risk_dist


@dataclass
class IctReport:
    trades: list[IctTrade] = field(default_factory=list)
    bars: int = 0
    setups_placed: int = 0
    setups_expired: int = 0
    funnel: dict = field(default_factory=dict)
    candidate_counts: list = field(default_factory=list)  # 各 setup 成立時的候選區域數

    def _funnel_text(self) -> str:
        if not self.funnel:
            return ""
        total = sum(self.funnel.values())
        rows = sorted(self.funnel.items(), key=lambda kv: -kv[1])
        seg = "\n".join(f"  {k}：{v}（{v/total*100:.0f}%）" for k, v in rows)
        return f"\n漏斗診斷（評估 {total} 次，各關卡擋掉次數）：\n{seg}"

    def _candidates_text(self) -> str:
        if not self.candidate_counts:
            return ""
        cc = sorted(self.candidate_counts)
        med = cc[len(cc) // 2]
        return (f"\nsetup 當下候選區域數：中位數 {med}、最大 {cc[-1]}"
                "（v1.1 修正後應為個位數）")

    def _sl_slip_text(self) -> str:
        """止損成交滑移統計：觸價模型下應趨近 0，收盤模型會系統性為正。"""
        slips = [t.sl_slip_r for t in self.trades
                 if t.closed and t.sl_slip_r is not None]
        if not slips:
            return ""
        avg = sum(slips) / len(slips)
        return (f"\n止損成交滑移（超越止損價，以 R 計）：平均 {avg:+.2f}R、"
                f"最大 {max(slips):+.2f}R（{len(slips)} 筆止損；觸價模型應趨近 0）")

    def verify_sl_fills(self) -> str:
        """§1 驗證輸出：逐筆列印止損家族出場的 entry/plan_sl/actual_exit/OHLC/R。"""
        rows = ["entry_px | plan_sl | actual_exit | OHLC(o,h,l,c) | r | slip_R"]
        for t in self.trades:
            if not t.closed or "sl" not in t.exit_reason or t.exit_px is None:
                continue
            o, h, low, c = t.trigger_ohlc or (0, 0, 0, 0)
            rows.append(
                f"{t.entry:.4f} | {t.sl_ref:.4f} | {t.exit_px:.4f} | "
                f"({o:.4f},{h:.4f},{low:.4f},{c:.4f}) | {t.r:+.2f} | "
                f"{t.sl_slip_r:+.2f}  [{t.exit_reason}]")
        return "\n".join(rows)

    def summary(self) -> str:
        closed = [t for t in self.trades if t.closed]
        if not closed:
            return (f"K 線 {self.bars} 根，掛單 {self.setups_placed} 次"
                    f"（{self.setups_expired} 次失效撤單），無成交。"
                    + self._candidates_text() + self._funnel_text())
        rs = [t.r for t in closed]
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        win_rate = len(wins) / len(rs)
        avg_w = sum(wins) / len(wins) if wins else 0.0
        avg_l = abs(sum(losses) / len(losses)) if losses else 0.0
        expectancy = win_rate * avg_w - (1 - win_rate) * avg_l
        gross_w = sum(wins)
        gross_l = abs(sum(losses))
        pf = gross_w / gross_l if gross_l else float("inf")
        lines = [
            f"成交 {len(closed)} 筆（掛單 {self.setups_placed}、失效 {self.setups_expired}）",
            f"勝率 {win_rate*100:.0f}%　平均賺 {avg_w:+.2f}R　平均賠 {-avg_l:+.2f}R",
            f"期望值 E = {expectancy:+.3f}R / 筆　獲利因子 {pf:.2f}　累積 {sum(rs):+.2f}R",
        ]
        # tags 分組（判斷哪些 confluence 真的有用）
        for tag in ("zone_kind", "in_ote", "killzone", "swept_pool"):
            groups: dict = {}
            for t in closed:
                groups.setdefault(str(t.tags.get(tag, "?")), []).append(t.r)
            if len(groups) > 1:
                seg = "　".join(
                    f"{k}: {sum(v)/len(v):+.2f}R×{len(v)}"
                    for k, v in sorted(groups.items()))
                lines.append(f"  依 {tag}：{seg}")
        return ("SL=觸價成交於止損價（gap 情形高估，需實盤 stop 單支撐）\n"
                + "\n".join(lines) + self._sl_slip_text()
                + self._candidates_text())


@dataclass
class Position:
    """持倉狀態（回測用），出場規則抽出成純函數方便單測。"""

    trade: IctTrade
    plan_sl: float
    qty_left: float
    tps_state: list          # [{price, fraction, filled}]
    be_done: bool = False


def apply_bar_exit(pos: Position, i: int, o: float, h: float, low: float,
                   c: float, *, slippage_pct: float = 0.0,
                   fee_rate: float = 0.0005, breakeven_after_tp: int = 1,
                   breakeven_offset_pct: float = 0.001,
                   same_bar: bool = False) -> bool:
    """對一根 K 套用出場規則，回傳 True 表示該筆已平倉。

    對稱成交模型（backtestfillfix.md §2）：
    - 止損觸價（low 觸及）先於 TP 判斷，成交在「止損價」而非收盤價。
    - 同根同時觸 SL 與 TP → 以 SL 計（worst）：SL 分支在前即實現此規則。
    - same_bar=True（進場當根）：只檢查止損、不吃 TP（保守），出場記
      sl_same_bar。
    - TP1 成交達 breakeven_after_tp 段 → 止損上移保本；之後保本被觸價
      打回自然成小正報酬。
    """
    tr = pos.trade
    if low <= pos.plan_sl:                       # 觸價止損（對稱於 TP）
        px = pos.plan_sl * (1 - slippage_pct)
        tr.pnl += (px - tr.entry) * pos.qty_left - px * pos.qty_left * fee_rate
        tr.closed, tr.closed_at = True, i
        tr.exit_reason = "sl_same_bar" if same_bar else "sl"
        tr.sl_ref, tr.exit_px = pos.plan_sl, px
        tr.trigger_ohlc = (o, h, low, c)
        pos.qty_left = 0.0
        return True
    if same_bar:                                 # 進場根只認止損，不吃 TP
        return False
    filled_n = sum(1 for t in pos.tps_state if t["filled"])
    for tp in pos.tps_state:
        if tp["filled"] or h < tp["price"]:
            continue
        q = min(tr.qty * tp["fraction"], pos.qty_left)
        tr.pnl += (tp["price"] - tr.entry) * q - tp["price"] * q * fee_rate
        pos.qty_left -= q
        tp["filled"] = True
        filled_n += 1
        if not pos.be_done and filled_n >= breakeven_after_tp > 0:
            pos.plan_sl = max(pos.plan_sl, tr.entry * (1 + breakeven_offset_pct))
            pos.be_done = True
    if pos.qty_left <= 1e-12:
        tr.closed, tr.closed_at = True, i
        tr.exit_reason = "tp_all"
        return True
    return False


def backtest_ict2022(entry_k, trigger_k, htf_k,
                     ict_cfg: Optional[dict] = None,
                     smc_cfg: Optional[dict] = None,
                     sessions_cfg: Optional[dict] = None,
                     fee_rate: float = 0.0005,
                     slippage_pct: float = 0.0,
                     risk_quote: float = 100.0,
                     breakeven_after_tp: int = 1,
                     breakeven_offset_pct: float = 0.001) -> IctReport:
    p = {**DEFAULTS, **(ict_cfg or {})}
    entry_int = str(p["entry_interval"])
    clock = SessionClock(sessions_cfg)
    tv = MtfView(trigger_k, str(p["trigger_interval"]))
    hv = MtfView(htf_k, str(p["htf_interval"]))

    rep = IctReport(bars=len(entry_k))
    pending: Optional[Setup] = None
    pending_bar = 0
    pending_trig_len = 0
    pos: Optional[Position] = None
    dead_keys: set = set()
    last_trig_len = -1
    exit_kw = dict(slippage_pct=slippage_pct, fee_rate=fee_rate,
                   breakeven_after_tp=breakeven_after_tp,
                   breakeven_offset_pct=breakeven_offset_pct)

    for i, k in enumerate(entry_k):
        ct = close_time(k, entry_int)
        if ct is None:
            continue
        trig = tv.upto(ct)
        htf = hv.upto(ct)
        o, hi, lo, cl = ohlc.o(k), ohlc.h(k), ohlc.l(k), ohlc.c(k)

        # --- 持倉管理（觸價止損先於 TP；同根衝突以 SL 計）---
        if pos is not None:
            if apply_bar_exit(pos, i, o, hi, lo, cl, **exit_kw):
                pos = None

        # --- 掛單管理 ---
        if pos is None and pending is not None:
            cancel = ""
            if cl > pending.range_high:
                cancel = "未回踩即創新高"
            elif i - pending_bar >= pending.expiry_bars:
                cancel = "掛單逾期"
            elif len(trig) > pending_trig_len:
                # 新 trigger K → 檢查反向 MSS
                st = structure.detect_structure(trig[-TRIGGER_WINDOW:])
                if any(e.kind == StructureKind.MSS
                       and e.direction == Direction.DOWN
                       and e.confirmed_at >= len(trig[-TRIGGER_WINDOW:])
                       - (len(trig) - pending_trig_len)
                       for e in st.events):
                    cancel = "反向 MSS"
                pending_trig_len = len(trig)
            if cancel:
                dead_keys.add(pending.key)
                rep.setups_expired += 1
                pending = None
            elif lo <= pending.limit_price:
                # 觸價成交
                entry_px = pending.limit_price
                qty = risk_quote / (entry_px - pending.stop_loss)
                trade = IctTrade(entry=entry_px, stop_loss=pending.stop_loss,
                                 qty=qty, risk_quote=risk_quote, opened_at=i,
                                 tags=dict(pending.tags))
                trade.pnl -= entry_px * qty * fee_rate
                pos = Position(trade=trade, plan_sl=pending.stop_loss,
                               qty_left=qty,
                               tps_state=[{"price": t.price,
                                           "fraction": t.fraction,
                                           "filled": False}
                                          for t in pending.take_profits])
                rep.trades.append(trade)
                dead_keys.add(pending.key)
                pending = None
                # 進場當根即觸止損（low 觸及）→ 立即以止損價成交 ≈ −1R
                if apply_bar_exit(pos, i, entry_px, hi, lo, cl,
                                  same_bar=True, **exit_kw):
                    pos = None

        # --- 找新 setup（空手、無掛單、新 trigger K 才重算）---
        if pos is None and pending is None and len(trig) != last_trig_len:
            last_trig_len = len(trig)
            ts = ohlc.t(k)
            kz = clock.in_killzone(float(ts)) if ts is not None else None
            s = find_setup(htf[-HTF_WINDOW:], trig[-TRIGGER_WINDOW:],
                           p, smc_cfg, killzone=kz, funnel=rep.funnel)
            if s is not None and s.key not in dead_keys:
                pending = s
                pending_bar = i
                pending_trig_len = len(trig)
                rep.setups_placed += 1
                rep.candidate_counts.append(
                    int(s.tags.get("candidates", 0)))

    return rep
