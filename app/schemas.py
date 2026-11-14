"""请求 / 响应数据模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    """分析接口入参。"""

    query: str = Field(..., description="股票代码或名称，如 600519 / 贵州茅台")
    question: str = Field(default="", description="用户自定义问题，可为空")


class LLMConfigRequest(BaseModel):
    """大模型 Key 配置入参。"""

    api_key: str = Field(default="", description="OpenAI 兼容 API Key")
    base_url: str = Field(default="https://api.deepseek.com", description="API Base URL")
    model: str = Field(default="deepseek-chat", description="模型名")
