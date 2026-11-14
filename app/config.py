"""应用配置：读取环境变量并构造 OpenAI 兼容客户端（默认对接 DeepSeek）。

支持运行时通过前端“设置 Key”更新，配置会同时写回 .env，无需重启服务。
换用其它厂商（Qwen / OpenAI 等）时，只需修改 Base URL 与模型名即可。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv, set_key
from openai import OpenAI

load_dotenv()

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


class Settings:
    """集中管理运行期配置（内存中的值优先，保存时写回 .env）。"""

    def __init__(self) -> None:
        self.api_key: str = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
        self.model: str = os.getenv("MODEL", "deepseek-chat").strip()
        self.llm_timeout: float = float(os.getenv("LLM_TIMEOUT", "60"))
        self.llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "1"))
        self.panda_username: str = os.getenv("PANDA_USERNAME", "").strip()
        self.panda_password: str = os.getenv("PANDA_PASSWORD", "").strip()
        self.host: str = os.getenv("HOST", "127.0.0.1").strip()
        self.port: int = int(os.getenv("PORT", "8000"))

    @property
    def is_llm_configured(self) -> bool:
        return bool(self.api_key) and self.api_key != "sk-your-key-here"

    @property
    def is_pandadata_configured(self) -> bool:
        return bool(self.panda_username and self.panda_password)

    def update_llm(self, api_key: str, base_url: str, model: str) -> None:
        """更新大模型配置并持久化到 .env。"""
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "https://api.deepseek.com").strip()
        self.model = (model or "deepseek-chat").strip()
        if _ENV_PATH.exists():
            set_key(str(_ENV_PATH), "DEEPSEEK_API_KEY", self.api_key)
            set_key(str(_ENV_PATH), "DEEPSEEK_BASE_URL", self.base_url)
            set_key(str(_ENV_PATH), "MODEL", self.model)


_settings = Settings()


def get_settings() -> Settings:
    return _settings


@lru_cache(maxsize=8)
def _client_for(api_key: str, base_url: str, timeout: float, max_retries: int) -> OpenAI:
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=max_retries,
    )


def get_client() -> OpenAI:
    """返回与当前配置匹配的 OpenAI 兼容客户端。"""
    s = get_settings()
    return _client_for(s.api_key, s.base_url, s.llm_timeout, s.llm_max_retries)
