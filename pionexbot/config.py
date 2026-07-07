"""讀取設定檔 (config.yaml) 與機密 (.env)。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv 為選用，缺少時改由系統環境變數提供
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


@dataclass
class Secrets:
    """從環境變數讀取的機密，永遠不寫進設定檔。"""

    api_key: str = ""
    api_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    line_channel_token: str = ""
    line_user_id: str = ""
    discord_webhook_url: str = ""
    webhook_secret: str = ""

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            api_key=os.getenv("PIONEX_API_KEY", "").strip(),
            api_secret=os.getenv("PIONEX_API_SECRET", "").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            line_channel_token=os.getenv("LINE_CHANNEL_TOKEN", "").strip(),
            line_user_id=os.getenv("LINE_USER_ID", "").strip(),
            discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", "").strip(),
            webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        )

    @property
    def has_api_keys(self) -> bool:
        return bool(self.api_key and self.api_secret)


@dataclass
class Config:
    """完整設定。raw 保留原始 dict 以利存取較少用到的欄位。"""

    mode: str = "paper"
    raw: dict[str, Any] = field(default_factory=dict)
    secrets: Secrets = field(default_factory=Secrets)

    # --- 區段快捷存取 ---
    @property
    def exchange(self) -> dict[str, Any]:
        return self.raw.get("exchange", {})

    @property
    def trading(self) -> dict[str, Any]:
        return self.raw.get("trading", {})

    @property
    def risk(self) -> dict[str, Any]:
        return self.raw.get("risk", {})

    @property
    def strategy(self) -> dict[str, Any]:
        return self.raw.get("strategy", {})

    @property
    def webhook(self) -> dict[str, Any]:
        return self.raw.get("webhook", {})

    @property
    def notify(self) -> dict[str, Any]:
        return self.raw.get("notify", {})

    @property
    def logging_cfg(self) -> dict[str, Any]:
        return self.raw.get("logging", {})

    @property
    def base_url(self) -> str:
        return self.exchange.get("base_url", "https://api.pionex.com")

    @property
    def symbol(self) -> str:
        return self.trading.get("symbol", "BTC_USDT")

    @property
    def is_live(self) -> bool:
        return self.mode.lower() == "live"

    def validate(self) -> list[str]:
        """回傳設定問題清單（空清單代表沒問題）。"""
        problems: list[str] = []
        if self.mode.lower() not in ("paper", "live"):
            problems.append(f"mode 必須是 paper 或 live，目前是 {self.mode!r}")
        if "_" not in self.symbol:
            problems.append(f"symbol 格式應為 BASE_QUOTE（如 BTC_USDT），目前是 {self.symbol!r}")
        if self.is_live and not self.secrets.has_api_keys:
            problems.append("live 模式需要在 .env 設定 PIONEX_API_KEY / PIONEX_API_SECRET")
        return problems


def load_config(path: str | os.PathLike[str] = "config.yaml",
                env_path: str | os.PathLike[str] = ".env") -> Config:
    """載入 .env 與 config.yaml，回傳 Config 物件。"""
    load_dotenv(env_path)
    secrets = Secrets.from_env()

    cfg_path = Path(path)
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    else:
        raw = {}

    mode = str(raw.get("mode", "paper"))
    return Config(mode=mode, raw=raw, secrets=secrets)
