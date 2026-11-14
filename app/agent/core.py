"""Agent 主流程：本地 A2A 投顾委员会 + 大模型综合研判 + SSE 流式输出。

事件协议（供后端 SSE 转发）：每次 yield 一个 dict：
- {"type": "status", "text": ...}       阶段提示
- {"type": "chart", "kind": "committee"/"technical"/"macro", "data": {...}}  图表数据
- {"type": "token", "text": ...}        报告/LLM 增量文本
- {"type": "error", "text": ...}        错误
- {"type": "done"}                       结束

接入真实大模型 Key 后，先本地跑完 A2A 投顾委员会并立刻流式输出完整报告，
再让大模型基于这份真实数据做综合研判；大模型失败也不会让用户拿不到结果。
"""
from __future__ import annotations

from typing import Any, Generator

from app.agent.prompts import SYSTEM_PROMPT
from app.config import get_client, get_settings


def _demo_stream(query: str, question: str) -> Generator[dict, None, None]:
    """未配置大模型 Key 时的降级：执行真实 A2A 委员会，用规则给出完整报告。"""
    from app.tools import macro as macro_tool
    from app.tools import buffett as buffett_tool
    from app.tools import technical as technical_tool
    from app.a2a import orchestrator as committee_tool

    yield {"type": "status", "text": "未检测到大模型 Key，进入本地规则模式（真实数据 + 大师规则研判）"}

    # 收集审议过程中的流式事件（大师初判 / 辩论交锋），按顺序实时推送
    a2a_events: list[dict] = []

    def _emit(kind, type_, payload):
        a2a_events.append({"type": type_, "kind": kind, "data": payload})

    committee = committee_tool.run_committee(query, question=question, emit=_emit)
    if committee["ok"]:
        # 先推送大师初判与辩论实况，再推送最终委员会图表
        for ev in a2a_events:
            if ev["kind"] in ("master", "debate"):
                yield {"type": "chart", "kind": ev["kind"], "data": ev["data"]}
        yield {"type": "chart", "kind": "committee", "data": committee["data"]["chart"]}
    else:
        committee = None

    buffett = buffett_tool.full_analysis(query)
    if buffett["ok"]:
        yield {"type": "chart", "kind": "buffett", "data": buffett["data"]["chart"]}
    else:
        buffett = None

    tech = technical_tool.analyze(query)
    if tech["ok"]:
        yield {"type": "chart", "kind": "technical", "data": tech["data"]["chart"]}
    macro = macro_tool.snapshot()
    if macro["ok"]:
        yield {"type": "chart", "kind": "macro", "data": macro["data"]["chart"]}

    if committee and committee["ok"]:
        for ln in committee_tool.committee_report_markdown(committee["data"]["summary"]).splitlines():
            yield {"type": "token", "text": ln + "\n"}
        yield {"type": "done"}
        return

    if buffett and buffett["ok"]:
        for ln in buffett_tool._summary_to_markdown(buffett["data"]["summary"]).splitlines():
            yield {"type": "token", "text": ln + "\n"}
        yield {"type": "done"}
        return

    lines = ["## 宏观面"]
    if macro["ok"]:
        lines.append(macro["data"]["summary"].get("market_bias") or "宏观数据获取中。")
        for r in macro["data"]["summary"].get("readings", [])[:6]:
            lines.append(f"- {r}")
    lines.append("\n## 技术面")
    if tech["ok"]:
        s = tech["data"]["summary"]
        lines.append(f"**{s['name']}（{s['symbol']}）** 收盘 {s['close']}，涨跌 {s['change_pct']}%")
        for sig in s.get("signals", []):
            lines.append(f"- {sig}")
    else:
        lines.append(f"技术数据获取失败：{tech['error']}")
    lines.append("\n## 综合结论")
    lines.append("（本地规则模式）请配置 DEEPSEEK_API_KEY 以获得大模型综合研判。")
    lines.append("\n## 风险提示")
    lines.append("市场有风险，数据来源于公开接口可能存在延迟。以上分析仅供研究学习，不构成任何投资建议。")

    for ln in lines:
        yield {"type": "token", "text": ln + "\n"}
    yield {"type": "done"}


def _yield_sync_charts(query: str) -> Generator[dict, None, None]:
    """推送与本次分析标的配套的右侧图表，保证左右两侧同步。"""
    from app.tools import buffett as buffett_tool
    from app.tools import macro as macro_tool
    from app.tools import technical as technical_tool

    buffett = buffett_tool.full_analysis(query)
    if buffett["ok"]:
        yield {"type": "chart", "kind": "buffett", "data": buffett["data"]["chart"]}
    tech = technical_tool.analyze(query)
    if tech["ok"]:
        yield {"type": "chart", "kind": "technical", "data": tech["data"]["chart"]}
    macro = macro_tool.snapshot()
    if macro["ok"]:
        yield {"type": "chart", "kind": "macro", "data": macro["data"]["chart"]}


def run_agent(query: str, question: str) -> Generator[dict, None, None]:
    """执行 Agent：本地委员会结果先行，大模型随后润色综合研判。"""
    settings = get_settings()
    if not settings.is_llm_configured:
        yield from _demo_stream(query, question)
        return

    from app.a2a import orchestrator as committee_tool

    # 第一步：本地 A2A 投顾委员会（真实数据，立即出结果）
    yield {"type": "status", "text": "A2A 投顾委员会计算中…"}
    a2a_events: list[dict] = []

    def _emit(kind, type_, payload):
        a2a_events.append({"type": type_, "kind": kind, "data": payload})

    committee = committee_tool.run_committee(query, question=question, emit=_emit)
    if committee["ok"]:
        summary = committee["data"]["summary"]
        # 先流式推送大师初判与辩论交锋，再推送最终委员会图表
        for ev in a2a_events:
            if ev["kind"] in ("master", "debate"):
                yield {"type": "chart", "kind": ev["kind"], "data": ev["data"]}
        yield {"type": "chart", "kind": "committee", "data": committee["data"]["chart"]}
        for ln in committee_tool.committee_report_markdown(summary).splitlines():
            yield {"type": "token", "text": ln + "\n"}
    else:
        summary = None
        yield {"type": "status", "text": f"本地委员会数据不足（{committee['error']}），尝试大模型直接分析"}

    # 同步推送技术面 / 宏观面 / 巴菲特专项图表，避免右侧停留在上一只股票
    yield from _yield_sync_charts(query)

    # 第二步：大模型基于真实委员会报告做综合研判
    yield {"type": "status", "text": "大模型综合研判中…（首次调用可能需要 20-60 秒）"}
    client = get_client()
    context = committee_tool.committee_report_markdown(summary) if summary else f"请分析股票：{query}"
    user_content = (
        "以下是 A2A 投顾委员会已经完成的真实数据报告：\n\n"
        f"{context}\n\n"
        "请作为投顾委员会主席，用简体中文输出最终投顾意见，结构为："
        "## 总管决议、## 各大师投顾建议、## 交易计划、## 风险提示。"
        "不要重复委员会报告里已经列出的详细数据，重点做分歧解读、仓位决策和可执行计划。"
    )
    if question.strip():
        user_content += f"\n\n用户补充问题：{question.strip()}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    try:
        yield from _final_answer(client, settings.model, messages)
        yield {"type": "done"}
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "text": f"大模型调用失败（上方本地委员会报告仍有效）：{e}"}
        yield {"type": "done"}


def _final_answer(client, model: str, messages: list[dict]) -> Generator[dict, None, None]:
    """让模型基于委员会报告流式产出最终研判。"""
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.4,
        max_tokens=1200,
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield {"type": "token", "text": delta.content}
