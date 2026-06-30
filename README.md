# 派網訊號交易機器人 · pionex-signal-bot

一個用 Python 寫的加密貨幣現貨交易機器人。接收交易訊號後，依風控規則自動在派網（Pionex）現貨市場下單。訊號來源可插拔——可以用內建技術指標策略、接 TradingView 的 webhook、或手動下單；下單與風控邏輯三者共用。

> ⚠️ **交易有風險，可能虧損本金。** 本專案預設為紙上交易（paper）模式，請務必先在 paper 模式充分測試、了解策略行為後，再考慮投入真實資金。作者不對任何交易損失負責。

---

## 我為什麼做這個

我做這個 bot，是想把「訊號」跟「執行」這兩件事拆開來。

我自己在 TradingView 上用 Pine Script 寫過一些策略（EMA / ADX / ATR 那類），但 TradingView 只能跳警報、不能真的下單。手動盯著警報再去交易所按買賣，一來會漏、二來人一緊張就會亂改規則。所以我想要一個東西：**不管訊號從哪來（我自己的指標、TradingView、還是手動），都走同一套風控、同一套下單流程，機械式執行。** 訊號歸訊號，執行歸執行，這樣我換策略時不必重寫下單邏輯，下單邏輯也只需要驗證一次。

## 我在意的幾個工程細節

這個專案我花最多心思的不是策略多炫，而是**怎麼讓自動下單不要出錯**，因為一旦接上真錢，一個重複下單的 bug 就是真實的虧損。幾個我刻意處理的點：

- **防止重複成交。** 我只用「已收盤」的 K 線評估訊號，而且每根 K 線只評估一次。原因是如果用「形成中」的 K 線，價格還在動，訊號會反覆跳進跳出，造成重複下單。這個坑我一開始沒想到，是測試時發現訊號亂跳才回頭修的。
- **單一部位、進出成對。** 空手才買、有倉才賣，持倉時不會一直加碼，避免越攤越平、爆掉資金。
- **多層風控。** 單筆金額上限、持倉上限、當日虧損上限、交易冷卻時間，全部在下單前檢查；任何一關不過就不下單。
- **paper / live 雙模式 + 介面隔離。** `PaperBroker`（模擬）和 `LiveBroker`（實盤）實作同一個介面，預設 paper，要切 live 是改設定、不是改程式，降低「測試跑得好好的、上線忘了改」的風險。
- **金鑰不進版控。** API key 走 `.env`，建立金鑰時建議只開「讀取 + 現貨交易」，不要開提現權限。

## 它能做什麼

- **內建技術指標策略**（免費、自主）：均線交叉、RSI、MACD、布林通道，可回測。
- **回測引擎**：用歷史 K 線重播策略，輸出報酬率、勝率、最大回撤、買入持有比較；含手續費、不使用槓桿、不做空，跟實盤同一套進出規則。
- **接 TradingView**：跑 webhook 伺服器，TradingView 警報發 JSON 過來就下單（用 `WEBHOOK_SECRET` 防陌生人觸發）。
- **自動網格**：在現價附近建網格機械式低買高賣，跌破區間自動平倉、再以新價位開新網格。
- **完整記錄與通知**：SQLite 記錄每筆交易與持倉、已實現損益；可推 Telegram / LINE 通知（啟動、成交、錯誤、每日損益摘要）。
- **容器化**：附 Dockerfile 與 docker-compose，可一鍵部署到雲端 VPS。

---

## 安裝與設定

```bash
pip install -r requirements.txt

cp config.example.yaml config.yaml
cp .env.example .env
```

在 `.env` 填入派網 API 金鑰（帳號 → API 管理建立，建議只勾「讀取」與「現貨交易」，切勿開「提現」）：

```
PIONEX_API_KEY=你的key
PIONEX_API_SECRET=你的secret
```

`config.yaml` 重點：`mode`（先用 `paper`）、`trading.symbol`、`trading.quote_per_trade`、`risk.*`。

## 使用

```bash
# 測試連線與 API 簽章（強烈建議第一步先跑）
python main.py test

# 查餘額 / 市價 / 狀態
python main.py balance
python main.py price
python main.py status

# 啟動內建策略機器人（定時抓 K 線、自動交易）
python main.py run-strategy

# 啟動 Webhook 伺服器（接 TradingView 等外部訊號）
python main.py run-webhook

# 手動下單
python main.py buy  --quote 20      # 市價買入 20 USDT
python main.py sell --base 0.001    # 市價賣出 0.001 BTC

# 回測（用歷史 K 線，不需 API 金鑰）
python main.py backtest --strategy macd --interval 1H --limit 1000

# 掃描最佳停損 / 停利
python main.py backtest-sweep --interval 5M --limit 1000
```

### 內建策略

| 名稱 | 邏輯 | 主要參數 |
|------|------|----------|
| `ma_cross` | 快線上穿慢線買、下穿賣 | `fast: 9`, `slow: 21` |
| `rsi` | RSI 上穿超賣線買、下穿超買線賣 | `period: 14`, `oversold: 30`, `overbought: 70` |
| `macd` | MACD 線上穿訊號線買、下穿賣 | `fast: 12`, `slow: 26`, `signal: 9` |
| `bollinger` | 跌破下軌買、突破上軌賣 | `period: 20`, `num_std: 2.0` |

> 順勢策略（如 `ma_cross`）通常關閉停利、留停損當安全網表現較好——它本來就靠死亡交叉讓獲利奔跑，硬加停利會砍太早。實際請以 `backtest-sweep` 在你自己的資料上的結果為準。

### 建議上線流程

1. `python main.py test` 確認簽章與連線正常。
2. `mode: paper` 跑 `run-strategy` 觀察幾天，看 `status` 的模擬損益。
3. 確認策略與風控符合預期後，**才**把 `config.yaml` 改成 `mode: live`，並先用很小的 `quote_per_trade` 實測。

---

## 接 TradingView

1. 啟動 `python main.py run-webhook`，讓這台機器有公開網址（ngrok / Cloudflare Tunnel / 部署到雲端）。
2. 在 `.env` 設一組 `WEBHOOK_SECRET`，避免被陌生人觸發下單。
3. TradingView 警報的 Webhook URL 填 `http(s)://你的網址:8080/webhook`，Message 填 JSON：

```json
{"secret": "你的密鑰", "action": "BUY", "symbol": "BTC_USDT", "quote_amount": 20}
```

平倉送 `{"secret": "你的密鑰", "action": "SELL", "symbol": "BTC_USDT"}`。
`action` 接受 `BUY/LONG/ENTRY` 與 `SELL/EXIT/CLOSE/SHORT`。

## Docker 部署

```bash
docker compose up -d strategy      # 只跑策略機器人
docker compose up -d webhook       # 只跑 Webhook 伺服器（開 8080 埠）
docker compose logs -f             # 看即時日誌
```

Webhook 對外請務必加反向代理 + HTTPS（Caddy / Nginx + Let's Encrypt），並設好 `WEBHOOK_SECRET`。`data/` 與 `logs/` 已掛載為 volume，容器重啟不會遺失交易記錄。

---

## 專案結構

```
main.py                  # CLI 入口
config.example.yaml      # 設定範本
Dockerfile / docker-compose.yml
pionexbot/
  config.py              # 讀設定 + 機密
  pionex_client.py       # 派網 REST 客戶端（HMAC 簽章 / 行情 / 下單）
  models.py              # Signal / OrderResult 等資料結構
  risk.py                # 風控檢查
  broker.py              # PaperBroker（模擬）/ LiveBroker（實盤）
  executor.py            # 訊號 → 風控 → 下單 → 記錄 / 通知
  backtest.py            # 回測引擎
  store.py               # SQLite 交易與持倉記錄
  notifier.py            # 日誌 + Telegram / LINE
  strategy/              # 內建策略 + indicators 工具
  sources/               # 訊號來源（strategy_runner、webhook）
tests/
  test_signing.py        # 簽章與風控測試
  test_strategies.py     # 策略與回測測試
```

## 新增自己的策略

在 `pionexbot/strategy/` 新增一個繼承 `Strategy` 的類別，實作 `evaluate(klines, symbol)` 回傳 `Signal` 或 `None`，再到 `strategy/__init__.py` 的 `_REGISTRY` 註冊名稱即可。

## 測試

```bash
python tests/test_signing.py      # 或 python -m pytest tests/ -v
```
