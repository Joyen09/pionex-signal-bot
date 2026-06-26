"""日誌與（選用）Telegram 通知。"""
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
    """統一的訊息出口：一定寫 log，啟用時也送 Telegram。"""

    def __init__(self, telegram_token: str = "", telegram_chat_id: str = "",
                 enabled: bool = False):
        self.token = telegram_token
        self.chat_id = telegram_chat_id
        self.enabled = enabled and bool(telegram_token and telegram_chat_id)

    def send(self, message: str, level: str = "info") -> None:
        getattr(logger, level, logger.info)(message)
        if self.enabled:
            self._send_telegram(message)

    def _send_telegram(self, message: str) -> None:
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
        except requests.RequestException as exc:
            logger.warning("Telegram 通知失敗：%s", exc)
