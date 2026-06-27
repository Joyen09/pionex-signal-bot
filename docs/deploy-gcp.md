# 在 Google Cloud 免費機器上部署派網訊號機器人

本文帶你從零把機器人跑在 Google Cloud 的**永久免費** e2-micro 虛擬機上。
整個過程約 30 分鐘。**全程先用 paper（紙上）模式**，確認沒問題再考慮實盤。

---

## 0. 你需要先準備

- 一個 Google 帳號
- 一張信用卡（僅供身分驗證；免費方案不會扣款，新帳號還送 90 天 US$300 試用額度）

---

## A. 註冊 Google Cloud

1. 開 <https://console.cloud.google.com>，用 Google 帳號登入。
2. 第一次會要你**啟用帳單**：填信用卡資料完成驗證。
   - 新帳號會拿到 **90 天 / US$300** 的試用額度。
   - e2-micro 機器本身屬於**永久免費 (Always Free)**，不吃額度。
3. 登入後，上方建立一個專案（Project），名字隨意，例如 `trading-bot`。

> 註冊比 Oracle 順很多，最後一步刷卡通常都過。若被拒，一樣先去銀行 App 開「國外/網路交易」。

---

## B. 建立免費的虛擬機 (VM)

左上選單 → **Compute Engine** → **VM 執行個體 (VM instances)** → **建立執行個體 (Create instance)**。

要**符合永久免費**，以下設定務必選對：

| 欄位 | 選擇 |
|---|---|
| 名稱 | `pionex-bot`（隨意） |
| 區域 Region | **us-west1（奧勒岡）** 或 us-central1（愛荷華）或 us-east1（南卡） ← 只有這三區的 e2-micro 免費 |
| 機器系列 Series | **E2** |
| 機器類型 Machine type | **e2-micro**（2 vCPU 共享 / 1GB 記憶體） |
| 開機磁碟 Boot disk | **Ubuntu 22.04 LTS**，標準磁碟，大小 **30 GB**（免費上限） |
| 防火牆 | 先**不用**勾 HTTP/HTTPS（策略機器人只需對外連線，不需對外開埠） |

> 若你之後要跑 **Webhook**（接 TradingView），才需要開埠 —— 那是進階情況，見文末附註。

按 **建立**，等一兩分鐘機器就會起來，列表會顯示一個**外部 IP**（記下來，等等設派網白名單用）。

---

## C. 連線到 VM

在 VM 列表那一列，點右邊的 **SSH** 按鈕 → 瀏覽器會直接開一個終端機視窗連進去。
（不用自己搞 SSH 金鑰，最方便。）

---

## D. 安裝 Docker

在 SSH 視窗裡貼上：

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

執行完**登出再重新 SSH 進來一次**（讓權限生效）。驗證：

```bash
docker --version
```

---

## E. 放上程式並設定

```bash
# 1. 把專案拉下來（換成你的 repo 網址）
git clone https://github.com/Joyen09/-.git bot
cd bot
git checkout claude/pionex-signal-bot-5ysba7   # 程式所在的分支

# 2. 建立設定檔
cp config.example.yaml config.yaml
cp .env.example .env
```

**編輯 `.env` 填金鑰**（⚠️ 直接在伺服器上填，不要經過 GitHub）：

```bash
nano .env
```
填入：
```
PIONEX_API_KEY=你的key
PIONEX_API_SECRET=你的secret
```
存檔：`Ctrl+O` → `Enter` → `Ctrl+X` 離開。

**編輯 `config.yaml` 選策略**（先保持 `mode: paper`）：
```bash
nano config.yaml
```
重點：`mode: paper`、`trading.symbol`、`strategy.name`、`trading.quote_per_trade`。

---

## F. 先驗證，再啟動（paper 模式）

```bash
# 驗證連線與 API 簽章（不會下單）
docker compose run --rm strategy test

# 沒問題就啟動策略機器人（背景執行）
docker compose up -d strategy

# 看即時日誌
docker compose logs -f strategy
```

看到「策略機器人啟動（紙上模式）」就成功了。按 `Ctrl+C` 只是離開看日誌，機器人仍在背景跑。

---

## G. 設定派網 API 的 IP 白名單（強烈建議）

1. 把 **B 步驟記下的 VM 外部 IP** 填進派網 → API 管理 → 該把 key 的「IP 地址權限」。
2. 這樣即使金鑰外洩，別人從其他 IP 也用不了。

> 注意：GCP 預設給的是「臨時外部 IP」，**只要你不停止 (Stop) VM，它就不會變**。若哪天 IP 變了，記得回派網更新白名單。需要永久固定可在 GCP 保留 (Reserve) 一個靜態 IP（附在運作中的機器上費用很低）。

---

## H. 常用維運指令

```bash
docker compose logs -f strategy     # 看日誌
docker compose restart strategy     # 重啟
docker compose down                 # 停止
docker compose up -d strategy       # 啟動

# 改了程式 / 拉新版後重建
git pull
docker compose up -d --build strategy

# 查機器人狀態（持倉、最近交易）
docker compose run --rm strategy status
```

---

## I. 確認真的在「免費」範圍

- 機器類型是 **e2-micro**、區域是 **us-west1 / us-central1 / us-east1**、磁碟 **≤30GB 標準磁碟** → 符合永久免費。
- 到「帳單 → 預算與快訊」設一個 **US$1 的預算提醒**，超過就寄信通知你，避免不小心開到要錢的資源。
- e2-micro 只有 1GB 記憶體，跑這隻機器人夠用。若覺得吃緊，可加一點 swap：
  ```bash
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  ```

---

## 附註：如果要跑 Webhook（接 TradingView）

1. 建立 VM 時（或事後在「VPC 網路 → 防火牆」）開放 **TCP 8080** 來自 TradingView。
2. 啟動 webhook 服務：`docker compose up -d webhook`
3. **務必**在 `.env` 設 `WEBHOOK_SECRET`，並建議加反向代理 + HTTPS（Caddy 最簡單）。
4. TradingView 的 Webhook URL 填 `http(s)://你的VM外部IP:8080/webhook`。

---

## 上線前最後提醒

- 先 **paper 模式跑幾天**，用 `status` 看模擬損益是否合理。
- 確認策略與風控符合預期，**才**把 `config.yaml` 改 `mode: live`，並先用很小的 `quote_per_trade` 實測。
- 交易有風險，請自行承擔。
