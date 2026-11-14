# -*- coding: utf-8 -*-
"""youzi_committee：游资委员会（21 位游资 Agent 的 A2A 协调器）。

与 orchestrator.run_committee（五位价值大师）同构、互补：

    run_committee        价值派·基本面/估值  → 中长线视角
    run_youzi_committee  游资派·超短/情绪    → 短线博弈视角

流程（全部复用既有 A2A 基建）：
1. 标的解析：复用 app.data.pandadata_client.resolve_symbol；
2. 逐标的信号评估：engine_bridge.evaluate_signals（21 位游资、真实行情、
   模块级行情缓存，仅 1 次 API 拉取）；
3. 初判：youzi_agent.YouziAgentNode.evaluate() 产出 21 份
   MasterResult 兼容结果（score/verdict/position/key_metrics）；
4. 审议：复用 deliberation.run_deliberation，质询用默认模板，
   辩护接入各游资节点的 YouziAgentNode.defend()（以其信号规则与
   历史回测战绩作答），产出 transcript + 影响力权重；
5. 综合：影响力加权评分/投票/仓位，产出委员会 MasterResult，
   经 orchestrator.committee_chart_json 转成与前端兼容的 chart。

对外返回与 run_committee 完全同构的 {ok, data:{chart, summary}}，
并在 data 中额外携带 signals / consensus / market 等游资专属字段。
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional

from . import engine_bridge
from .deliberation import (
    DeliberationConfig,
    apply_influence_to_scores,
    run_deliberation,
)
from .protocol import build_master_registry
from .youzi_agent import COMMITTEE_ID, YouziAgentNode

COMMITTEE_NAME = "游资委员会"


# ----------------------- 标的解析 ----------------------- #

def _fallback_symbol_code(query: str) -> Optional[str]:
    """query 中直接出现 6 位代码时，按交易所规则补后缀。"""
    m = re.search(r"\b([03568]\d{5})\b", query)
    if not m:
        return None
    code = m.group(1)
    if code.startswith(("6", "68", "9")):
        return f"{code}.SH"
    if code.startswith(("0", "3", "2")):
        return f"{code}.SZ"
    if code.startswith(("8", "4")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def resolve_query_symbol(query: str) -> Optional[str]:
    """优先复用宿主的名称->代码解析，失败时回退到裸代码识别。"""
    try:
        from ..data.pandadata_client import resolve_symbol
        res = resolve_symbol(query)
        if isinstance(res, dict):
            data = res.get("data") or {}
            sym = data.get("symbol")
            if res.get("ok") and sym:
                return str(sym)
            if isinstance(sym, str) and sym:
                return sym
    except Exception:
        pass
    return _fallback_symbol_code(query)


# ----------------------- 综合决议 ----------------------- #

def _synthesize_youzi(
    results: List[Dict[str, Any]],
    influence: Dict[str, float],
    deliberated: bool,
    symbol: str,
    market: Dict[str, Any],
) -> Dict[str, Any]:
    num = sum((r.get("score", 0) or 0) * influence.get(r.get("agent_id"), 1.0) for r in results)
    den = sum(influence.get(r.get("agent_id"), 1.0) for r in results) or 1.0
    weighted_score = round(num / den, 1)

    votes = {"买入": 0, "观望": 0, "回避": 0}
    for r in results:
        v = r.get("verdict", "观望")
        votes[v if v in votes else "观望"] += 1
    majority = max(votes, key=votes.get)

    buyers = [r for r in results if r.get("verdict") == "买入"]
    # 游资风格：总仓位 = 各买入者建议仓位的均值（超短轻仓），再乘以买入占比共识度
    avg_pos = sum(r.get("position", 0) for r in buyers) / len(buyers) if buyers else 0.0
    consensus_ratio = len(buyers) / max(1, len(results))
    position = round(min(0.6, avg_pos * (0.5 + 0.5 * consensus_ratio)), 2)
    cash = round(1.0 - position, 2)

    tone = "up" if majority == "买入" and weighted_score >= 60 else ("down" if majority == "回避" or weighted_score < 45 else "mid")

    # 展示层用原始 score（influence 归一化到 21 人后 weighted_score 数值过小不直观）
    sorted_by_score = sorted(results, key=lambda r: r.get("score", 0), reverse=True)
    _s = lambda r: r.get("score", 0)  # noqa: E731
    bulls = [f"{r['name']}({_s(r):.0f})" for r in sorted_by_score[:3]]
    bears = [f"{r['name']}({_s(r):.0f})" for r in sorted_by_score[-2:] if r.get("verdict") == "回避"]

    price_txt = f"{market.get('close', '-')} 元（{market.get('pct_chg', 0):+.2f}%，{market.get('date', '')}）" if market else ""
    verdict = (
        f"游资委员会（{'经多轮审议' if deliberated else '初判'}）对 {symbol} "
        f"{price_txt} 给出【{majority}】：{votes['买入']} 票买入 / {votes['观望']} 票观望 / "
        f"{votes['回避']} 票回避，加权评分 {weighted_score:.0f}/100，短线建议仓位 ≤{int(position * 100)}%。"
    )
    reason = (
        f"主攻手：{'、'.join(bulls) if bulls else '无'}。"
        + (f" 空仓/反对观点：{'、'.join(bears)}。" if bears else "")
        + f" 超短体系信号来自 21 位游资的规则化策略（打板/低吸/接力/情绪周期），"
          f"共识度 {consensus_ratio:.0%}，注意隔日兑现纪律。"
    )

    committee = {
        "name": COMMITTEE_NAME,
        "role": "A2A 游资派协调者（21 位超短游资）",
        "verdict": verdict,
        "score": weighted_score,
        "tone": tone,
        "key_metrics": [
            {"label": "加权评分", "value": f"{weighted_score:.0f}"},
            {"label": "投票", "value": f"{votes['买入']}买/{votes['观望']}观/{votes['回避']}避"},
            {"label": "短线仓位", "value": f"≤{int(position * 100)}%"},
            {"label": "主攻手", "value": bulls[0] if bulls else "-"},
        ],
        "reason": reason,
        "allocation_position": position,
        "allocation_cash": cash,
        "action": majority,
        "conflicts": [
            f"多头（{'、'.join(bulls[:2])}）与谨慎方（{'、'.join(bears) if bears else '观望派'}）在 {symbol} 的短线节奏上存在分歧"
        ] if (bulls and (bears or votes["回避"] > 3)) else [],
        "allocation": {"position": position, "cash": cash},
        "agent_scores": [
            {"name": r.get("name"), "score": r.get("score", 0), "tone": r.get("tone", "mid")}
            for r in sorted_by_score
        ],
    }
    return committee


# ----------------------- 对外主入口 ----------------------- #

def run_youzi_committee(
    query: str,
    question: str = "",
    *,
    extra_symbols: Optional[List[str]] = None,
    signal_days: Optional[int] = None,
    enable_deliberation: bool = True,
    with_backtest_stats: bool = True,
    emit: Optional[Callable[[str, str, Any], None]] = None,
) -> Dict[str, Any]:
    """运行游资委员会，返回 {ok, data:{chart, summary}}（与 run_committee 同构）。"""
    symbol = resolve_query_symbol(query)
    if not symbol:
        return {"ok": False, "error": f"无法解析标的「{query}」，请用名称或 6 位代码"}

    symbols = [symbol] + [s for s in (extra_symbols or []) if s and s != symbol]

    # 1) 逐标的信号评估（21 位游资 × 真实行情）
    sig = engine_bridge.evaluate_signals(symbols, signal_days=signal_days)
    market = sig["market"].get(symbol, {})

    # 2) 回测画像（首次运行 21 个回测并持久化，之后秒级读取）
    stats: Dict[str, dict] = {}
    if with_backtest_stats:
        try:
            stats = engine_bridge.get_backtest_stats()
        except Exception:
            stats = {}

    # 3) 21 个 A2A 节点初判（MasterResult 兼容）
    agents_meta = engine_bridge.load_youzi_agents()
    nodes: Dict[str, YouziAgentNode] = {}
    results: List[Dict[str, Any]] = []
    for name, meta in agents_meta.items():
        agent_signals = sig["agents"].get(name)
        if not agent_signals:
            continue
        node = YouziAgentNode(
            agent_meta=meta,
            symbol_signals=agent_signals["signals"].get(symbol, {}),
            symbol=symbol,
            market_ctx=market,
            backtest_stats=stats.get(name),
        )
        nodes[node.card.agent_id] = node
        results.append(node.evaluate())
    if not results:
        return {"ok": False, "error": "游资信号评估结果为空"}
    if emit:
        for r in results:
            emit("youzi", "master", r)

    # 4) 多轮审议（质询：默认模板；辩护：游资节点以规则+战绩作答）
    influence: Dict[str, float] = {r["agent_id"]: 1.0 for r in results}
    transcript: List[Any] = []
    engagement: Dict[str, Dict[str, int]] = {}
    rounds_played = 0
    if enable_deliberation:
        def _defense_fn(target: Dict[str, Any], challenge) -> str:
            node = nodes.get(target.get("agent_id"))
            if node is None:
                return ""
            return node.defend(challenge).text()

        delib = run_deliberation(
            results,
            config=DeliberationConfig(max_rounds=2, top_conflicts=3),
            defense_fn=_defense_fn,
            on_round=lambda rnd, msgs: (
                emit("youzi_debate", "debate", [m.to_dict() for m in msgs]) if emit else None
            ),
        )
        influence = delib.influence
        transcript = [m.to_dict() for m in delib.transcript]
        engagement = delib.engagement
        rounds_played = delib.rounds
        results = apply_influence_to_scores(results, influence)

    # 5) 综合决议 + 组装委员会（chart 兼容字段一应俱全）
    committee = _synthesize_youzi(results, influence, enable_deliberation, symbol, market)
    committee["agents"] = results
    committee["debate"] = transcript
    committee["influence"] = influence
    committee["engagement"] = engagement
    committee["debate_rounds"] = rounds_played
    committee["agent_cards"] = build_master_registry(results).to_dict()
    committee["query"] = query
    committee["question"] = question
    committee["symbol"] = symbol

    from .orchestrator import committee_chart_json
    chart = committee_chart_json(committee)
    if emit:
        emit("youzi_committee", "committee", chart)

    return {
        "ok": True,
        "data": {
            "chart": chart,
            "summary": committee,
            "mode": "youzi",
            "symbol": symbol,
            "market": sig["market"],
            "window": sig["window"],
            "signal_days": sig["signal_days"],
            "evaluated_at": sig["evaluated_at"],
            "consensus": sig["consensus"],
            "signals": {
                name: a["signals"].get(symbol, {})
                for name, a in sig["agents"].items()
            },
            "errors": sig["errors"],
        },
    }


# ----------------------- 多标的扫描（轻量） ----------------------- #

def run_youzi_screen(
    symbols: List[str],
    signal_days: Optional[int] = None,
) -> Dict[str, Any]:
    """只做逐标的信号矩阵与共识排名，不进入审议流程（供筛选/调度用）。"""
    if not symbols:
        return {"ok": False, "error": "symbols 为空"}
    sig = engine_bridge.evaluate_signals(symbols, signal_days=signal_days)
    ranking = sorted(sig["consensus"].items(), key=lambda kv: kv[1]["score"], reverse=True)
    return {
        "ok": True,
        "data": {
            "mode": "youzi_screen",
            "window": sig["window"],
            "signal_days": sig["signal_days"],
            "evaluated_at": sig["evaluated_at"],
            "market": sig["market"],
            "consensus": sig["consensus"],
            "ranking": [{"symbol": s, **c} for s, c in ranking],
            "errors": sig["errors"],
        },
    }
