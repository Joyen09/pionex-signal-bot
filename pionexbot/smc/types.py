"""SMC 共用型別。

索引慣例：所有 index / confirmed_at 都是「K 線陣列的整數索引」；
time 欄位為該根 K 線的時間戳（有就帶，沒有就是 None）。
confirmed_at 是事件「確認」的那根 K —— 下游策略只能在確認之後行動。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class SwingKind(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"


class StructureKind(str, Enum):
    BOS = "BOS"    # 趨勢延續（break of structure）
    MSS = "MSS"    # 換手／反轉（market structure shift, 又稱 CHoCH）


class PoolSide(str, Enum):
    BUY_SIDE = "BUY_SIDE"    # swing high 上方的買方流動性（空單止損聚集）
    SELL_SIDE = "SELL_SIDE"  # swing low 下方的賣方流動性（多單止損聚集）


class PoolSource(str, Enum):
    SWING = "SWING"
    EQ = "EQ"            # 等高/等低（多個 swing 合併）
    PDH_PDL = "PDH_PDL"  # 前日高低
    SESSION = "SESSION"  # 時段高低


class ZoneKind(str, Enum):
    OB = "OB"            # 訂單塊
    PB = "PB"            # 推進塊（趨勢延續中的同向 OB）
    BREAKER = "BREAKER"  # 被翻轉的 OB
    FVG = "FVG"          # 失衡缺口
    IFVG = "IFVG"        # 被翻轉的 FVG
    BPR = "BPR"          # 多空 FVG 交集


class ZoneState(str, Enum):
    FRESH = "FRESH"
    TESTED = "TESTED"
    FLIPPED = "FLIPPED"
    FILLED = "FILLED"


@dataclass
class SwingPoint:
    index: int                 # swing 所在的 K 線索引
    time: Optional[Any]
    price: float
    kind: SwingKind
    confirmed_at: int          # 右側 R 根收盤後才確認（= index + right）


@dataclass
class StructureEvent:
    kind: StructureKind
    direction: Direction       # UP = 向上突破 / DOWN = 向下突破
    broken_level: float        # 被突破的價位
    swing_ref: SwingPoint      # 被突破的 swing
    confirmed_at: int          # 突破「收盤確認」的那根 K
    time: Optional[Any] = None


@dataclass
class LiquidityPool:
    price: float
    side: PoolSide
    source: PoolSource
    members: list[SwingPoint] = field(default_factory=list)
    created_at: int = 0        # 池成形（最後一個成員確認）的索引
    consumed: bool = False     # 被收盤有效突破且未收回 → 不再作為目標
    consumed_at: Optional[int] = None


@dataclass
class SweepEvent:
    pool: LiquidityPool
    wick_extreme: float        # 掃過去的影線極值（之後作 SL 錨點）
    confirmed_at: int
    time: Optional[Any] = None


@dataclass
class Zone:
    kind: ZoneKind
    top: float
    bottom: float
    direction: Direction       # UP = 看漲區（等回踩買）；DOWN 鏡像
    created_at: int
    state: ZoneState = ZoneState.FRESH
    state_changed_at: Optional[int] = None
    time: Optional[Any] = None

    @property
    def ce(self) -> float:
        """Consequent Encroachment：區域中點（講義的 FVG 0.5 進場價）。"""
        return (self.top + self.bottom) / 2

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top
