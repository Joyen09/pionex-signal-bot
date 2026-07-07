"""把所有元件組裝起來，供 CLI 使用。"""
from __future__ import annotations

from .broker import Broker, LiveBroker, PaperBroker
from .config import Config
from .executor import Executor
from .notifier import Notifier, setup_logging
from .pionex_client import PionexClient
from .risk import RiskManager
from .store import Store


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        log = cfg.logging_cfg
        setup_logging(log.get("level", "INFO"), log.get("file"))

        self.client = PionexClient(
            api_key=cfg.secrets.api_key,
            api_secret=cfg.secrets.api_secret,
            base_url=cfg.base_url,
        )
        tg = cfg.notify.get("telegram", {})
        line = cfg.notify.get("line", {})
        discord = cfg.notify.get("discord", {})
        self.notifier = Notifier(
            telegram_token=cfg.secrets.telegram_bot_token,
            telegram_chat_id=cfg.secrets.telegram_chat_id,
            telegram_enabled=bool(tg.get("enabled", False)),
            line_token=cfg.secrets.line_channel_token,
            line_user_id=cfg.secrets.line_user_id,
            line_enabled=bool(line.get("enabled", False)),
            discord_webhook_url=cfg.secrets.discord_webhook_url,
            discord_enabled=bool(discord.get("enabled", False)),
            discord_bot_token=cfg.secrets.discord_bot_token,
            discord_channel_id=cfg.secrets.discord_channel_id,
            discord_user_id=cfg.secrets.discord_user_id,
            discord_bot_enabled=bool(discord.get("enabled", False)),
        )
        self.store = Store("data/bot.db")
        self.risk = RiskManager(cfg.risk)
        self.broker: Broker = self._make_broker()
        self.executor = Executor(self.broker, self.risk, self.store, self.notifier)

    def _make_broker(self) -> Broker:
        if self.cfg.is_live:
            return LiveBroker(self.client)
        return PaperBroker(self.client)

    def close(self) -> None:
        self.store.close()
