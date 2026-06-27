# 派網訊號機器人 (Pionex Signal Bot)

一個用 Python 寫的派網（Pionex）自動交易機器人。接收**交易訊號**後，依風控規則自動在派網現貨市場下單。

> ⚠️ **交易有風險，可能虧損本金。** 本專案預設為**紙上交易（paper）模式**，請務必先在 paper 模式充分測試，了解策略行為後，再考慮投入真實資金。作者不對任何交易損失負責。

## 特色

- 🧩 **模組化訊號來源**：下單與風控邏輯共用，訊號來源可插拔
  - **內建技術指標策略**（免費、自主）：均線交叉、RSI、MACD、布林通道
  - **Webhook 接收**：接 TradingView 警報或任何能發 HTTP POST 的來源
  - **手動下單**：CLI 直接買賣
- 📊 **回測**：用歷史 K 線重播策略，輸出報酬率、勝率、最大回撤、買入持有比較
- 🐳 **容器化**：附 Dockerfile 與 docker-compose，一鍵部署到雲端
- 🛡️ **安全優先**
  - 預設 **paper 模式**（只模擬與記錄，不下真實單）
  - **風控**：單筆金額上限、持倉上限、當日虧損上限、交易冷卻
  - API 金鑰放 `.env`，不進版控
- 📒 **完整記錄**：SQLite 記錄每筆交易與持倉、已實現損益
- 🔔 **通知**：日誌 + 選用 Telegram
- 🔭 **預留合約**：架構以介面隔離，未來可擴充永續合約

## 安裝

```bash
pip install -r requirements.txt
```

## 設定（3 步）

1. **複製設定範本**
   ```bash
   cp config.example.yaml config.yaml
   cp .env.example .env
   ```

2. **填入 API 金鑰**（編輯 `.env`）
   到派網網頁 → 帳號 → API 管理 建立金鑰。
   > 建議只勾選「讀取」與「現貨交易」，**切勿開啟「提現」權限**。
   ```
   PIONEX_API_KEY=你的key
   PIONEX_API_SECRET=你的secret
   ```

3. **調整交易參數**（編輯 `config.yaml`）
   重點：`mode`（先用 `paper`）、`trading.symbol`、`trading.quote_per_trade`、`risk.*`。

## 使用

```bash
# 第一步：測試連線與 API 簽章（強烈建議）
python main.py test

# 查餘額 / 查市價 / 查狀態
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
python main.py backtest                              # 用 config 裡的策略
python main.py backtest --strategy macd --interval 1H --limit 1000
python main.py backtest --strategy rsi --cash 1000
```

## 內建策略

| 名稱 | 邏輯 | 主要參數（`config.yaml` 的 `strategy.params`） |
|---|---|---|
| `ma_cross` | 快線上穿慢線買、下穿賣 | `fast: 9`, `slow: 21` |
| `rsi` | RSI 上穿超賣線買、下穿超買線賣 | `period: 14`, `oversold: 30`, `overbought: 70` |
| `macd` | MACD 線上穿訊號線買、下穿賣 | `fast: 12`, `slow: 26`, `signal: 9` |
| `bollinger` | 跌破下軌買、突破上軌賣 | `period: 20`, `num_std: 2.0` |

切換策略：改 `config.yaml` 的 `strategy.name` 與 `strategy.params`。
回測時可用 `--strategy` 臨時指定，不必改設定檔。

## 回測

```bash
python main.py backtest --strategy ma_cross --interval 5M --limit 1000
```

輸出範例：

```
════════ 回測結果 ════════
  交易對       : BTC_USDT
  策略         : ma_cross
  K 線根數     : 1000
  策略總報酬   : +5.32%
  買入持有報酬 : +12.10%
  最大回撤     : 3.40%
  完成交易次數 : 18
  勝率         : 61.1%
  平均每筆損益 : +2.95
══════════════════════════
```

> 回測只抓公開 K 線，**不需要 API 金鑰**。回測假設以「當根收盤價」成交、含手續費、不使用槓桿、不做空，結果僅供參考，實盤會有滑點與延遲。

## Docker 部署

```bash
# 準備好 .env 與 config.yaml 後：
docker compose up -d strategy      # 只跑策略機器人
docker compose up -d webhook       # 只跑 Webhook 伺服器（開 8080 埠）
docker compose up -d               # 兩者都跑
docker compose logs -f             # 看即時日誌
```

📘 **完整雲端部署教學**：[docs/deploy-gcp.md](docs/deploy-gcp.md)（Google Cloud 永久免費機器，從註冊到上線的中文步驟）

部署到雲端（VPS）建議：
1. 機器裝好 Docker 與 Docker Compose。
2. 上傳專案、填好 `.env`（金鑰）與 `config.yaml`。
3. Webhook 對外請務必加**反向代理 + HTTPS**（如 Caddy / Nginx + Let's Encrypt），並設好 `WEBHOOK_SECRET`。
4. `data/` 與 `logs/` 已掛載為 volume，容器重啟不會遺失交易記錄。

### 建議上線流程

1. `python main.py test` 確認簽章與連線都正常。
2. `mode: paper` 跑 `run-strategy` 觀察幾天，看 `python main.py status` 的模擬損益。
3. 確認策略與風控符合預期後，**才**把 `config.yaml` 改成 `mode: live`，並先用很小的 `quote_per_trade` 實測。

## 接 TradingView

1. 啟動 `python main.py run-webhook`，並讓這台機器有公開網址（可用 ngrok、Cloudflare Tunnel 或部署到雲端）。
2. 在 `.env` 設一組 `WEBHOOK_SECRET`，避免被陌生人觸發下單。
3. TradingView 警報的 **Webhook URL** 填 `http(s)://你的網址:8080/webhook`。
4. 警報 **Message** 填 JSON：
   ```json
   {"secret": "你的密鑰", "action": "BUY", "symbol": "BTC_USDT", "quote_amount": 20}
   ```
   平倉：
   ```json
   {"secret": "你的密鑰", "action": "SELL", "symbol": "BTC_USDT"}
   ```
   `action` 接受 `BUY/LONG/ENTRY` 與 `SELL/EXIT/CLOSE/SHORT`。

## 專案結構

```
main.py                  CLI 入口
config.example.yaml      設定範本
Dockerfile               容器映像
docker-compose.yml       一鍵部署（strategy / webhook）
pionexbot/
  config.py              讀設定 + 機密
  pionex_client.py       派網 REST 客戶端（HMAC 簽章/行情/下單）
  models.py              Signal / OrderResult 等資料結構
  risk.py                風控檢查
  broker.py              PaperBroker（模擬）/ LiveBroker（實盤）
  executor.py            訊號 → 風控 → 下單 → 記錄/通知
  backtest.py            回測引擎
  store.py               SQLite 交易與持倉記錄
  notifier.py            日誌 + Telegram
  strategy/              內建策略 + indicators 工具
    ma_cross / rsi / macd / bollinger
  sources/               訊號來源（strategy_runner、webhook）
tests/
  test_signing.py        簽章與風控測試
  test_strategies.py     策略與回測測試
```

## 新增自己的策略

在 `pionexbot/strategy/` 新增一個繼承 `Strategy` 的類別，實作 `evaluate(klines, symbol)` 回傳 `Signal` 或 `None`，再到 `strategy/__init__.py` 的 `_REGISTRY` 註冊名稱即可。

## 測試

```bash
python tests/test_signing.py      # 或 python -m pytest tests/ -v
```

## 免責聲明

本軟體僅供學習與研究，不構成投資建議。加密貨幣交易風險極高，請自行評估並承擔所有風險。
