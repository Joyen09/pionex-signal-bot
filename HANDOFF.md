# 交接文件（HANDOFF）— 給下一個 Claude Code session

> 讀我就夠了。這份文件是上一個 session（2026-07-12 結束）留給你的完整狀態轉移。
> 使用者：初學者，用繁體中文，偏好「一步一步可複製貼上的 VM 指令」。
> 使用者的 VM：GCP 免費 e2-micro，帳號 `linjoyen`，用 GCP 網頁 SSH 操作。

---

## 1. 現在什麼在跑（真錢！小心！）

| 部署 | 位置 | 分支 | 模式 | 內容 |
|---|---|---|---|---|
| **實盤網格** | VM `~/bot` | `main` | **live（真錢 ~108 USDT）** | `docker compose up -d grid`，BTC_USDT 8 格網格，累計已實現 +1.4↑ |
| 紙上測試 | VM `~/bot-paper` | `claude/pionex-signal-bot-5ysba7` | paper | `docker-compose.paper.yml`（獨立容器名/8081 埠/獨立 data） |

**鐵則（violating these hurts real money）：**
- `~/bot` 的 `data/bot.db` **絕對不能刪**——存著實盤網格的持倉追蹤
- **不要**叫使用者在派網 App 手動賣網格買的 BTC——會打亂機器人記帳
- `.env` / `config.yaml` 被 gitignore，**永遠不進版控**；API key 只開「讀取+現貨交易」，無提現權限
- 使用者的 **Telegram 曾被盜**，已停用（`.env` 清空 + config enabled: false），通知已全面改 **Discord**——不要建議重開 Telegram

## 2. Discord 通知架構

- 實盤網格：`~/bot/.env` 的 `DISCORD_WEBHOOK_URL`（推播）＋ `DISCORD_BOT_TOKEN`/`DISCORD_CHANNEL_ID`/`DISCORD_USER_ID`（雙向：頻道打字→回網格狀態，唯讀）→ 頻道 `#pionex`
- 紙上：同一隻 bot、不同頻道 `#paper`（webhook URL 留空、走 bot 發訊 fallback）
- 設計：`notifier.py` 的 send() 有 Webhook 優先、無則 Bot 的 fallback；LINE 支援存在但未啟用

## 3. Git 狀態

- **repo**：`Joyen09/pionex-signal-bot`（GitHub 上已改名；本地 remote 可能仍叫 `-`）
- **main**：實盤網格跑的版本（Phase 0：網格/DCA/四策略/Discord）
- **分支 `claude/pionex-signal-bot-5ysba7`**：SMC 規格書 Phase 1+2+3 全部完成，**PR #1 開著待 merge**
  - merge 不影響既有行為（向下相容測試全綠，40+ 項測試）
  - merge 後實盤更新：`cd ~/bot && git pull && docker compose up -d --build grid`（網格行為不變）
- 沙盒 `git push` 偶爾 HTTP 503——重試幾次或改用 GitHub API（mcp github push_files）

## 4. SMC 專案的完整結論（重要：別重做已做過的事）

規格書：使用者上傳的 `smcspec.md`（Phase 1 風控 / Phase 2 偵測 / Phase 3 ict2022 / Phase 4 工具）。

**已完成並驗收：**
- Phase 1：以止損定倉位、多段 TP+保本、SL 收盤觸發（同根以 SL 計）、每日上限、消息封鎖、webhook 擴充。使用者親手 curl 測過 paper 全流程 ✅
- Phase 2：`pionexbot/smc/`（structure/liquidity/zones/sessions/mtf/plot），使用者拿講義截圖逐張人工驗收過（BOS/MSS、OB/Breaker、FVG/IFVG/BPR/CE 定義全部核對一致）✅
- Phase 3：`strategy/ict2022.py`（八步流水線純函數）＋ `backtest_ict.py`（R 倍數統計＋漏斗診斷）＋ `sources/ict_runner.py`（run-ict）✅

**回測最終結果（2026-07，六幣各 ~35 天 5M，zone_edge/killzone off/min_rr 1.2，零滑價）：**
BTC +2.79R×1｜ETH +1.69R×2｜SOL −1.79R×7｜BNB 0 筆（HTF 空頭正確空手）｜DOGE −1.32R×1｜XRP −0.01R×4
→ **合計 15 筆、+1.36R、E≈+0.09R/筆 ≈ 零**。事先判準（≥20 筆且 E 明顯>0）**未通過**。
**結論已告知使用者：證據不支持投錢。ict2022 不上實盤。**

**假設驗證結果（2026-07-13，六幣同段資料、`require_ote: true` + `sweep_sources: ["EQ"]` 硬條件重回測）：**
BTC 0 筆（原 +2.79R 的贏單被篩掉）｜ETH 0 筆（原 +1.69R×2 被篩掉）｜SOL +4.30R×3｜BNB 0 筆｜DOGE −1.43R×1｜XRP −3.01R×2
→ **合計 6 筆、−0.14R、E ≈ −0.02R/筆**。之前的贏單根本不來自 OTE+EQ 型態，假設被推翻；SOL 3/3 全勝是小樣本假象（n=3）。
**最終結論（已告知使用者）：ict2022 兩輪誠實驗證期望值皆 ≈ 零，此線收案，不上實盤。除非使用者主動要求，不要再提議繼續優化 ict2022。**

## 5. 這段開發抓到的坑（下次別再踩）

1. **派網 K 線單次回傳是新→舊**——`pionex_client.get_klines` 已統一 `_sort_asc` 排序，別移除
2. **派網歷史有保存上限**（5M 約 1 萬根 ≈35 天），翻頁過界回 `MARKET_INVALID_TIME`——`get_klines_history` 已優雅停手
3. 派網週期代號用 `60M` 不是 `1H`（client 有別名轉換）；市價買最低 `minAmount=10 USDT`；BTC `basePrecision=6`，賣出數量要無條件捨去
4. **plotly `add_shape` 是 O(n²)**——shapes/annotations 必須收集後一次 `update_layout`
5. **SQLite 連線不能跨執行緒**——背景執行緒要開自己的 Store
6. Docker：compose 的 entrypoint 已是 `python main.py`，執行子指令用 `docker compose run --rm <service> <子指令>`（不要再帶 python main.py）；`container_name` 寫死，平行部署用 `docker-compose.paper.yml`
7. 市價單回應只有 orderId，成交明細要輪詢 `get_order`；只認 `filledSize/filledAmount>0` 欄位

## 6. 常用指令速查（在對應目錄下）

```bash
# 測試（無網路需求）
python tests/test_signing.py && python tests/test_strategies.py \
  && python tests/test_phase1.py && python tests/test_smc.py && python tests/test_ict2022.py

# 實盤網格（~/bot，main）
docker compose logs -f grid
docker compose run --rm grid status          # 交易記錄
# Discord #pionex 打任意字 → 回網格狀態

# 紙上（~/bot-paper，分支）
docker compose -f docker-compose.paper.yml run --rm webhook ict-backtest --limit 20000 [--symbol ETH_USDT]
docker compose -f docker-compose.paper.yml run --rm webhook smc-plot --interval 15M --limit 1000 --out data/smc.html
docker compose -f docker-compose.paper.yml run --rm webhook run-ict   # ict2022 paper 執行
```

## 7. 下一步的候選（使用者說「看要不要繼續實測」）

按優先序建議：
1. **merge PR #1**（安全，收工具進 main）
2. ~~ict2022 硬條件重回測~~ **已做完，假設被推翻（見第 4 節），此線收案**。開關（`require_ote`/`sweep_sources`）留在程式裡供未來參考
3. 使用者 VM 的 `~/bot-paper` 目前切在 `claude/handoff-review-j1ujnz` 分支、config.yaml 開著兩個硬條件——若之後要跑其他 paper 測試，記得把 `require_ote`/`sweep_sources` 改回關閉
3. 使用者還想做的其他機器人：台股、每日便宜機票搜尋、OCR 圖片辨識（都想用同一隻 Discord bot 架構）
4. 網格照顧：跌破 60369 會自動平倉重開（設計行為）；BTC 現處 2026 熊市，使用者知道 4 年週期論

## 8. 溝通風格提醒

- 使用者是初學者：指令給完整可貼、解釋用生活比喻、一次不要丟太多步驟
- **對「能不能賺錢」永遠誠實**——這是整個專案的核心約定。使用者已被教育過：n=1 不是證據、倖存者偏差、事先判準。請延續這個標準，不要為了討好而樂觀
- 使用者會貼截圖回報，擅長跟著漏斗/報表判讀——善用既有的診斷工具而不是猜
