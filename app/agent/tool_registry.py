"""工具注册：把 Python 工具描述成 OpenAI function-calling 的 JSON schema，
并提供名称 -> 可调用对象的分发表。

约定：每个工具返回 {"ok", "data": {"summary", "chart"}, "error"}。
- summary 会被序列化后回灌给 LLM 阅读；
- chart 由编排层收集后单独推给前端，不进入 LLM 上下文（节省 token）。
"""
from __future__ import annotations

from app.tools import macro as macro_tool
from app.tools import buffett as buffett_tool
from app.tools import technical as technical_tool
from app.a2a import orchestrator as committee_tool

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "technical_analysis",
            "description": "获取指定 A 股股票的技术分析：均线、MACD、RSI、KDJ、BOLL、量能与信号判断。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "股票代码或名称，如 600519 或 贵州茅台",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "macro_snapshot",
            "description": "获取当前中国宏观经济指标（GDP/CPI/PPI/PMI/M2）与大盘指数近期走势快照。无需参数。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buffett_full_analysis",
            "description": "对指定 A 股做巴菲特式完整价值投资分析：企业质量与护城河、估值与安全边际、管理层与股东回报、市场时机、仓位建议。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "股票代码或名称，如 600519 / 贵州茅台",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buffett_market_timing",
            "description": "获取沪深300近6年 PE/PB 估值百分位与指数趋势，判断市场处于贪婪还是恐惧区间。无需参数。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buffett_screen",
            "description": "按巴菲特标准筛选指数股票池（ROE、盈利质量、低杠杆、估值），返回综合评分最高的候选股票。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "string",
                        "description": "指数代码，默认 000300（沪深300），也支持 000905/000852/000016 等",
                        "enum": ["000300", "000905", "000852", "000016", "000510", "399006"],
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "返回前 N 只，默认 20",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "a2a_investment_committee",
            "description": "启动 A2A 投顾委员会：总览管家（总管 Agent）解析任务后，并行派发给巴菲特、彼得·林奇、菲利普·费雪、杰西·利弗莫尔、本杰明·格雷厄姆五个独立 Agent，分别给出投顾建议，再由总管汇总分歧并给出最终仓位与交易计划。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "股票代码或名称，如 600519 / 贵州茅台",
                    }
                },
                "required": ["query"],
            },
        },
    },
]


def _wrap(result: dict) -> dict:
    """把纯 dict 结果包成工具层标准结构。"""
    return {"ok": True, "error": None, "data": {"summary": result, "chart": None}}


def dispatch(name: str, arguments: dict) -> dict:
    """按名称执行工具，返回工具结果 dict。"""
    if name == "technical_analysis":
        return technical_tool.analyze(arguments.get("query", ""))
    if name == "macro_snapshot":
        return macro_tool.snapshot()
    if name == "buffett_full_analysis":
        return buffett_tool.full_analysis(arguments.get("query", ""))
    if name == "buffett_market_timing":
        return _wrap(buffett_tool.market_timing())
    if name == "buffett_screen":
        return buffett_tool.screen(
            index=arguments.get("index", "000300"),
            top_n=int(arguments.get("top_n") or 20),
        )
    if name == "a2a_investment_committee":
        return committee_tool.run_committee(arguments.get("query", ""))
    return {"ok": False, "error": f"未知工具：{name}", "data": None}
