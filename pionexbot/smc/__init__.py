"""SMC（Smart Money Concepts）偵測模組。

純函數設計：輸入 K 線陣列 → 輸出事件／區域物件，不依賴全域狀態。
所有事件的時間一律記在「確認當根」（no lookahead / no repaint）。
"""
from .types import (LiquidityPool, StructureEvent, SweepEvent, SwingPoint,  # noqa: F401
                    Zone)
