"""派網訊號機器人 (Pionex Signal Bot)。

模組化結構：
    config         讀取設定與機密
    pionex_client  派網 REST API 客戶端（簽章/行情/下單）
    models         Signal / OrderResult 等資料結構
    risk           風控檢查
    broker         紙上 / 實盤下單介面
    executor       把訊號轉成下單動作
    strategy/      內建技術指標策略
    sources/       訊號來源（策略輪詢、Webhook）
    notifier       日誌與通知
    store          交易記錄與狀態（SQLite）
    bot            組裝以上元件
"""

__version__ = "0.1.0"
