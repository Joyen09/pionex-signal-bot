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
        self.notifier = Notifier(
            telegram_token=cfg.secrets.telegram_bot_token,
            telegram_chat_id=cfg.secrets.telegram_chat_id,
            enabled=bool(tg.get("enabled", False)),
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
