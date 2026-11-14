"""A2A 投资委员会协调器（Orchestrator）。

总管 Agent（Supervisor）把同一份数据包并行派发给五位大师 Agent，
每位大师独立给出 verdict/score/position。随后进入多轮审议（Deliberation）：
大师 Agent 之间互相质询与辩护，最终总管综合"辩论后的影响力权重"给出决议。

对外保持与原前端/CLI 完全兼容的接口：
    run_committee(query, question="") -> {"ok", "data": {"chart", "summary"}}
    committee_report_markdown(summary) -> str
并在 chart / summary 中附带 debate（辩论记录）、influence（影响力权重）、
agent_cards（Agent 名片），供前端展示真正的 agent-to-agent 协作过程。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List, Optional

from .deliberation import DeliberationConfig, apply_influence_to_scores, run_deliberation
from .protocol import build_master_registry
from .masters import (
    run_buffett,
    run_lynch,
    run_fisher,
    run_livermore,
    run_graham,
)

VALID_ACTIONS = {"买入", "持有", "观望", "回避"}


def _build_ctx(query: str) -> Optional[Dict[str, Any]]:
    """通过真实的巴菲特数据层构造完整数据包（ctx）。"""
    from ..tools import buffett as buffett_tool

    try:
        res = buffett_tool.full_analysis(query)
    except Exception:
        return None
    if not res.get("ok"):
        return None
    return res.get("data", {}).get("summary")


# ----------------------- 单轮大师初判 ----------------------- #

def _run_masters(ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    masters = []
    for fn in (run_buffett, run_lynch, run_fisher, run_livermore, run_graham):
        try:
            r = fn(ctx)
            if r:
                masters.append(r)
        except Exception as e:  # noqa: BLE001
            masters.append({
                "agent": getattr(fn, "__name__", "master"),
                "name": fn.__name__.replace("run_", "").title(),
                "score": 0,
                "verdict": "观望",
                "tone": "mid",
                "action": "等待更多数据",
                "reason": f"分析出错：{e}",
                "key_metrics": [],
                "error": str(e),
            })
    # 统一补充 agent_id（用于审议注册）
    for m in masters:
        m.setdefault("agent_id", m.get("agent") or m.get("name"))
    return masters


# ----------------------- 总管综合（Synthesis） ----------------------- #

def _weighted_avg(masters: List[Dict[str, Any]], influence: Dict[str, float]) -> float:
    """用影响力权重对各大师原始评分做一次加权平均（不重复加权）。"""
    num = sum((m.get("score", 0) or 0) * influence.get(m.get("agent_id", m.get("name")), 1.0)
              for m in masters)
    den = sum(influence.get(m.get("agent_id", m.get("name")), 1.0) for m in masters) or 1.0
    return num / den


def _vote(masters: List[Dict[str, Any]]) -> Dict[str, int]:
    tally: Dict[str, int] = {"买入": 0, "持有": 0, "观望": 0, "回避": 0}
    for m in masters:
        a = m.get("action") or "观望"
        # action 形如 "建议买入" / "买入" / "回避风险"
        for k in tally:
            if k in str(a):
                tally[k] += 1
                break
    return tally


def _detect_conflicts(masters: List[Dict[str, Any]]) -> List[str]:
    ups = [m.get("name") for m in masters if m.get("tone") == "up"]
    downs = [m.get("name") for m in masters if m.get("tone") == "down"]
    conflicts: List[str] = []
    if ups and downs:
        conflicts.append(
            f"乐观派（{('、'.join(ups))}）与谨慎派（{('、'.join(downs))}）在方向判断上存在分歧"
        )
    return conflicts


def _synthesize(
    masters: List[Dict[str, Any]],
    influence: Dict[str, float],
    deliberated: bool,
) -> Dict[str, Any]:
    avg = _weighted_avg(masters, influence)
    votes = _vote(masters)
    majority = max(votes, key=votes.get)
    weighted_score = round(avg, 1)
    position = max(0.0, min(1.0, round(weighted_score / 100.0, 2)))
    cash = round(1.0 - position, 2)
    conflicts = _detect_conflicts(masters)

    tone = "up" if majority in ("买入", "持有") and avg >= 55 else ("down" if majority == "回避" or avg < 45 else "mid")
    verdict = (
        f"委员会（{'经多轮审议' if deliberated else '初判'}）多数建议【{majority}】，"
        f"加权评分 {weighted_score:.0f}/100，建议仓位约 {int(position * 100)}%。"
    )
    reason = (
        f"{majority} 为最高票动作（{votes[majority]}/{len(masters)} 票）。"
        + (f" 存在 {len(conflicts)} 处明显分歧，已在辩论中交锋。" if conflicts else " 各派观点基本一致。")
    )

    committee = {
        "name": "总览管家",
        "role": "A2A 协调者",
        "verdict": verdict,
        "score": weighted_score,
        "tone": tone,
        "key_metrics": [
            {"label": "加权评分", "value": f"{weighted_score:.0f}"},
            {"label": "多数动作", "value": majority},
            {"label": "建议仓位", "value": f"{int(position * 100)}%"},
            {"label": "现金保留", "value": f"{int(cash * 100)}%"},
        ],
        "reason": reason,
        "allocation_position": position,
        "allocation_cash": cash,
        "action": majority,
    }
    committee["agents"] = masters
    committee["conflicts"] = conflicts
    committee["allocation"] = {"position": position, "cash": cash}
    committee["agent_scores"] = [
        {"name": m.get("name"), "score": m.get("weighted_score", m.get("score", 0)), "tone": m.get("tone", "mid")}
        for m in masters
    ]
    return committee


# ----------------------- 对外主入口 ----------------------- #

def run_committee(
    query: str,
    question: str = "",
    *,
    enable_deliberation: bool = True,
    deliberation_config: Optional[DeliberationConfig] = None,
    llm_call: Optional[Callable[[str, str], str]] = None,
    emit: Optional[Callable[[str, str, Any], None]] = None,
) -> Dict[str, Any]:
    """运行 A2A 投顾委员会，返回统一结构 {ok, data:{chart, summary}}。

    参数：
        enable_deliberation: 是否启用多轮审议（默认开启，向后兼容）。
        deliberation_config: 审议参数（轮数/冲突数等）。
        llm_call: 大模型回调，供质询/辩护生成更自然的措辞。
        emit: (kind, type, payload) 通用事件发射器，可用于 SSE 流式推送。
    """

    ctx = _build_ctx(query)
    if not ctx:
        return {"ok": False, "error": f"未能获取标的「{query}」的数据包"}

    # 1) 总管广播任务给五位大师（这里以"独立初判"体现并行派发）
    broadcast = (
        f"任务：对标的「{ctx.get('name', query)}」"
        f"{('（' + question + '）') if question else ''}"
        "给出独立投资判断。请基于数据包独立分析，不要相互参考。"
    )

    # 2) 收集初判
    masters = _run_masters(ctx)
    if emit:
        emit("broadcast", "broadcast", {"text": broadcast})
        for m in masters:
            emit("master", "master", m)

    # 3) 多轮审议（大师 Agent 互相质询/辩护）
    influence: Dict[str, float] = {m.get("agent_id", m.get("name")): 1.0 for m in masters}
    debate_transcript: List[Any] = []
    engagement: Dict[str, Dict[str, int]] = {}
    rounds_played = 0
    if enable_deliberation:
        delib = run_deliberation(
            masters,
            config=deliberation_config or DeliberationConfig(),
            llm_call=llm_call,
            on_round=lambda rnd, msgs: (
                emit("debate_round", "debate", [m.to_dict() for m in msgs]) if emit else None
            ),
        )
        influence = delib.influence
        debate_transcript = [m.to_dict() for m in delib.transcript]
        engagement = delib.engagement
        rounds_played = delib.rounds
        masters = apply_influence_to_scores(masters, influence)

    # 4) 总管综合决议
    committee = _synthesize(masters, influence, enable_deliberation)
    committee["debate"] = debate_transcript
    committee["influence"] = influence
    committee["engagement"] = engagement
    committee["debate_rounds"] = rounds_played
    committee["agent_cards"] = build_master_registry(masters).to_dict()
    committee["query"] = query
    committee["question"] = question
    committee["name"] = ctx.get("name", query)
    committee["symbol"] = ctx.get("symbol", query)

    chart = committee_chart_json(committee)
    summary = committee
    if emit:
        emit("committee", "committee", chart)

    return {"ok": True, "data": {"chart": chart, "summary": summary}}


# ----------------------- 图表 JSON（兼容前端） ----------------------- #

def committee_chart_json(committee: Dict[str, Any]) -> Dict[str, Any]:
    pos = committee.get("allocation_position", 0.0)
    cash = committee.get("allocation_cash", 1.0 - pos)
    agents = committee.get("agents", [])
    return {
        "name": committee.get("name", "组合"),
        "symbol": committee.get("symbol", ""),
        "query": committee.get("query", ""),
        "verdict": committee.get("verdict", ""),
        "score": committee.get("score", 0),
        "tone": committee.get("tone", "mid"),
        "action": committee.get("action", ""),
        "position": pos,
        "cash": cash,
        "allocation": {"position": round(pos, 2), "cash": round(cash, 2)},
        "agents": [
            {
                "agent": a.get("agent") or a.get("agent_id"),
                "name": a.get("name"),
                "school": a.get("school", ""),
                "role": a.get("role", ""),
                "color": a.get("color", "#f0b90b"),
                "score": a.get("weighted_score", a.get("score", 0)),
                "verdict": a.get("verdict", ""),
                "action": a.get("action", ""),
                "position": a.get("position", 0.0),
                "key_metrics": a.get("key_metrics", []),
                "reason": a.get("reason", ""),
                "watch_items": a.get("watch_items", []),
                "tone": a.get("tone", "mid"),
                "influence": a.get("influence", 1.0),
            }
            for a in agents
        ],
        "conflicts": committee.get("conflicts", []),
        "debate": committee.get("debate", []),
        "influence": committee.get("influence", {}),
        "engagement": committee.get("engagement", {}),
        "debate_rounds": committee.get("debate_rounds", 0),
        "agent_cards": committee.get("agent_cards", []),
        "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
    }


# ----------------------- Markdown 报告（兼容 CLI / SSE） ----------------------- #

def committee_report_markdown(summary: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("## 总管决议")
    lines.append(f"- 结论：{summary.get('verdict', '')}")
    lines.append(f"- 加权评分：{summary.get('score', 0):.0f}/100  仓位：{int(summary.get('allocation_position', 0)*100)}%  现金：{int(summary.get('allocation_cash', 0)*100)}%")
    if summary.get("conflicts"):
        lines.append("\n## 主要分歧")
        for c in summary["conflicts"]:
            lines.append(f"- {c}")
    if summary.get("debate"):
        lines.append(f"\n## 多轮审议（{summary.get('debate_rounds', 0)} 轮，{len(summary['debate'])} 条交锋）")
        for m in summary["debate"]:
            tag = "质询" if m.get("role") == "challenge" else "辩护"
            lines.append(f"- 【{tag}】{m.get('sender')} → {m.get('receiver')}：{m.get('text', '')}")
    lines.append("\n## 各大师投顾建议")
    for a in summary.get("agents", []):
        infl = a.get("influence", 1.0)
        lines.append(
            f"- **{a.get('name')}**（{a.get('school', '')}）：{a.get('verdict')} | "
            f"评分 {a.get('weighted_score', a.get('score', 0)):.0f} | 影响力 {infl:.2f}"
        )
    lines.append("\n## 风险提示")
    lines.append("市场有风险，数据来源于公开接口可能存在延迟。以上分析仅供研究学习，不构成任何投资建议。")
    return "\n".join(lines)


def committee_summary(query: str, question: str = "") -> Dict[str, Any]:
    """兼容旧接口别名。"""
    res = run_committee(query, question)
    return res["data"]["summary"] if res["ok"] else {}


# --------------------------------------------------------------------------- #
# 游资委员会注册（新玩法入口；不影响上方既有 run_committee / 图表 / 报告流程） #
# --------------------------------------------------------------------------- #

def get_committee_registry() -> Dict[str, Any]:
    """A2A 委员会联合注册表：价值大师委员会 + 游资委员会。

    这是"游资委员会注册进 Orchestrator"的注册点：既有委员会照常工作，
    新委员会通过本注册表对外提供能力发现。
    """
    registry: List[Dict[str, Any]] = [
        {
            "committee_id": "value_masters",
            "name": "价值大师委员会",
            "agents": ["buffett", "lynch", "fisher", "livermore", "graham"],
            "agent_count": 5,
            "style": "价值派·基本面/估值（中长线）",
            "entrypoint": "app.a2a.orchestrator.run_committee",
            "api": "/api/a2a/committee",
        },
        {
            "committee_id": "youzi_committee",
            "name": "游资委员会",
            "agents": [],
            "agent_count": 0,
            "style": "游资派·超短/情绪（短线）",
            "entrypoint": "app.a2a.youzi_committee.run_youzi_committee",
            "api": "/api/a2a/youzi",
        },
    ]
    try:  # 游资侧懒加载（避免影响宿主启动速度与既有行为）
        from . import engine_bridge
        cards = engine_bridge.youzi_registry_cards()
        registry[1]["agents"] = [c.agent_id for c in cards]
        registry[1]["agent_count"] = len(cards)
    except Exception as exc:
        registry[1]["error"] = f"{type(exc).__name__}: {exc}"
    return {"ok": True, "committees": registry}


async def run_council(
    query: str,
    question: str = "",
    *,
    enable_deliberation: bool = True,
    deliberation_config: Optional[DeliberationConfig] = None,
    youzi_signal_days: Optional[int] = None,
    emit: Optional[Callable[[str, str, Any], None]] = None,
) -> Dict[str, Any]:
    """双院合议：价值大师委员会（中长线） × 游资委员会（短线）。

    两院并行独立审议，再按 50/50 融合终裁；输出与 run_committee
    同构（{ok, data:{chart, summary}}），并在 data 中携带两院原始报告。
    """
    import asyncio

    from .youzi_committee import run_youzi_committee

    masters_res, youzi_res = await asyncio.gather(
        asyncio.to_thread(
            run_committee,
            query,
            question=question,
            enable_deliberation=enable_deliberation,
            deliberation_config=deliberation_config,
            emit=emit,
        ),
        asyncio.to_thread(
            run_youzi_committee,
            query,
            question=question,
            signal_days=youzi_signal_days,
            enable_deliberation=enable_deliberation,
            emit=emit,
        ),
        return_exceptions=True,
    )
    if isinstance(masters_res, Exception):
        masters_res = {"ok": False, "error": f"{type(masters_res).__name__}: {masters_res}"}
    if isinstance(youzi_res, Exception):
        youzi_res = {"ok": False, "error": f"{type(youzi_res).__name__}: {youzi_res}"}

    m_ok = bool(masters_res.get("ok"))
    y_ok = bool(youzi_res.get("ok"))
    if not (m_ok or y_ok):
        return {"ok": False,
                "error": f"两院均失败：{masters_res.get('error')} | {youzi_res.get('error')}"}

    m_data = masters_res.get("data") or {}
    y_data = youzi_res.get("data") or {}
    m_score = float((m_data.get("summary") or {}).get("score") or 50)
    y_score = float((y_data.get("summary") or {}).get("score") or 50)
    total = (m_score + y_score) / 2 if (m_ok and y_ok) else (m_score if m_ok else y_score)

    verdict = "买入" if total >= 70 else ("回避" if total < 45 else "观望")

    m_summary = m_data.get("summary") or {}
    y_summary = y_data.get("summary") or {}
    m_agents = m_summary.get("agents") or []
    y_agents = y_summary.get("agents") or []
    m_top = max(m_agents, key=lambda a: a.get("score", 0))["name"] if m_agents else "-"
    y_top = max(y_agents, key=lambda a: a.get("score", 0))["name"] if y_agents else "-"
    m_pos = float(m_summary.get("allocation_position") or 0)
    y_pos = float(y_summary.get("allocation_position") or 0)
    position = round((m_pos + y_pos) / 2 if (m_ok and y_ok) else (m_pos if m_ok else y_pos), 2)

    agree = ""
    if m_ok and y_ok:
        m_v = str(m_summary.get("verdict", ""))
        y_v = str(y_summary.get("action") or "")
        if "买入" in m_v and y_v == "买入":
            agree = "两院共识偏多"
        elif "回避" in m_v and y_v == "回避":
            agree = "两院共识回避"
        else:
            agree = "两院存在期限错配：价值派看长线质地，游资派看短线情绪"

    council = {
        "name": "双院合议",
        "role": "价值大师委员会（中长线） × 游资委员会（短线）",
        "verdict": f"双院合议终裁【{verdict}】：价值派 {m_score:.0f} 分 / 游资派 {y_score:.0f} 分，"
                   f"融合评分 {total:.0f}/100，建议总仓位 ≤{int(position * 100)}%。{agree}。",
        "score": round(total, 1),
        "tone": "up" if total >= 70 else ("down" if total < 45 else "mid"),
        "key_metrics": [
            {"label": "价值派", "value": f"{m_score:.0f} 分" if m_ok else "失败"},
            {"label": "游资派", "value": f"{y_score:.0f} 分" if y_ok else "失败"},
            {"label": "融合", "value": f"{total:.0f} 分｜仓位 ≤{int(position * 100)}%"},
            {"label": "两院最强", "value": f"{m_top} / {y_top}"},
        ],
        "reason": "价值大师看企业质地与估值安全边际（中长线）；游资委员会看超短信号与情绪周期"
                  "（隔日兑现）。两者期限不同，分歧本身即信息：同向看多时可中线短打结合，"
                  "分歧时以各自期限内的纪律执行。",
        "allocation_position": position,
        "allocation_cash": round(1 - position, 2),
        "action": verdict,
        "conflicts": [],
        "allocation": {"position": position, "cash": round(1 - position, 2)},
        "agent_scores": [],
        "agents": [],
        "debate": [],
        "influence": {},
        "engagement": {},
        "debate_rounds": 0,
        "agent_cards": {},
        "query": query,
        "question": question,
        "symbol": m_data.get("symbol") or y_data.get("symbol"),
    }
    chart = committee_chart_json(council)
    if emit:
        emit("council", "committee", chart)
    return {
        "ok": True,
        "data": {
            "chart": chart,
            "summary": council,
            "mode": "council",
            "masters": masters_res,
            "youzi": youzi_res,
        },
    }
