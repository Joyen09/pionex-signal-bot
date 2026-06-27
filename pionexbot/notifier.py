"""日誌與通知（Telegram、LINE）。

- 一律寫 log。
- Telegram：免費、無限量，啟用後所有訊息都推。
- LINE：用 Messaging API 推播（LINE Notify 已於 2025 停用）。
  LINE 免費額度有限，所以**預設只推「重要」訊息**（實際成交、錯誤），
  例行訊號/被風控擋下的訊息不推，避免洗版與耗用額度。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("pionexbot")


def setup_logging(level: str = "INFO", file: Optional[str] = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if file:
        Path(file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


class Notifier:
    """統一訊息出口。send() 一律寫 log，並依設定推 Telegram / LINE。"""

    def __init__(self,
                 telegram_token: str = "", telegram_chat_id: str = "",
                 telegram_enabled: bool = False,
                 line_token: str = "", line_user_id: str = "",
                 line_enabled: bool = False):
        self.tg_token = telegram_token
        self.tg_chat = telegram_chat_id
        self.tg_enabled = telegram_enabled and bool(telegram_token and telegram_chat_id)

        self.line_token = line_token
        self.line_user = line_user_id
        self.line_enabled = line_enabled and bool(line_token and line_user_id)
        self._last_line_msg: Optional[str] = None  # 簡單去重，避免重複錯誤洗版

    def send(self, message: str, level: str = "info", important: bool = False,
             push: bool = True) -> None:
        """一律寫 log。push=False 則只記錄、不外推（給頻繁的網格成交用）。
        push=True 時：Telegram 一律推；LINE 僅在 important 或 error 時推。"""
        getattr(logger, level, logger.info)(message)
        if not push:
            return
        if self.tg_enabled:
            self._send_telegram(message)
        if self.line_enabled and (important or level == "error"):
            self._send_line(message)

    def get_updates(self, offset: Optional[int] = None, timeout: int = 0) -> list:
        """讀取 Telegram 新訊息（getUpdates）。回傳 update 物件 list。"""
        if not self.tg_enabled:
            return []
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.tg_token}/getUpdates",
                params=params, timeout=timeout + 15)
            return resp.json().get("result", [])
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Telegram getUpdates 失敗：%s", exc)
            return []

    def _send_telegram(self, message: str) -> None:
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                json={"chat_id": self.tg_chat, "text": message},
                timeout=10,
            )
        except requests.RequestException as exc:
            logger.warning("Telegram 通知失敗：%s", exc)

    def _send_line(self, message: str) -> None:
        # 連續相同訊息只推一次（例如 API 短暫故障時的重複錯誤）
        if message == self._last_line_msg:
            return
        self._last_line_msg = message
        try:
            resp = requests.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": f"Bearer {self.line_token}",
                    "Content-Type": "application/json",
                },
                json={"to": self.line_user,
                      "messages": [{"type": "text", "text": message[:4900]}]},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning("LINE 通知失敗 (HTTP %s)：%s",
                               resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            logger.warning("LINE 通知失敗：%s", exc)
