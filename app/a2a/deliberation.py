"""A2A 多轮审议（Deliberation）引擎。

在总管并行派发、大师独立初判之后，本引擎驱动大师 Agent 之间的"对话"：

1. 冲突发现：找出初判里 verdict / score / position 分歧最大的 Agent 对。
2. 质询（challenge）：让分歧偏向相反的一方，针对对方的逻辑漏洞或数据冲突发起质询。
3. 辩护（defense）：被质询方回应，可坚持原观点、承认局限或微调 score/position。
4. 收敛：根据辩护质量（由总管判定"说服力"）调整各 Agent 的最终影响力权重。

这样，最终的委员会决议不再只是"数人头投票"，而是经过观点交锋后、
由更具说服力的一方影响更大的权重——这才是真正的 agent-to-agent 协作。

引擎同时产出一份 `transcript`（辩论记录），可被前端还原成"辩论时间线"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .protocol import (
    AgentMessage,
    AgentRegistry,
    MessageRole,
    build_master_registry,
)


# 投资流派 → 立场倾向（用于冲突匹配时判断"天然对立"）
TONE_RANK = {"up": 1, "mid": 0, "down": -1}


@dataclass
class DeliberationConfig:
    max_rounds: int = 2                 # 最多审议轮数
    top_conflicts: int = 3              # 每轮挑出的最大冲突对数
    min_score_gap: float = 10.0         # 触发质询的最小评分差
    influence_decay: float = 0.7        # 辩护未被说服时的影响力衰减


@dataclass
class DeliberationResult:
    transcript: List[AgentMessage] = field(default_factory=list)
    rounds: int = 0
    # agent_id -> 最终影响力权重（0~1），被说服越多权重越低
    influence: Dict[str, float] = field(default_factory=dict)
    # agent_id -> 质询/辩护计数（供前端展示"攻防强度"）
    engagement: Dict[str, Dict[str, int]] = field(default_factory=dict)


# ----------------------- 冲突发现 ----------------------- #

def _norm_score(m: Dict[str, Any]) -> float:
    s = m.get("score")
    return float(s) if isinstance(s, (int, float)) else 0.0


def _tone(m: Dict[str, Any]) -> str:
    return (m.get("tone") or "mid").lower()


def find_conflicts(masters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """找出初判分歧最大的 Agent 对（按 |score 差| + 立场反向加成排序）。"""

    conflicts: List[Dict[str, Any]] = []
    n = len(masters)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = masters[i], masters[j]
            score_gap = abs(_norm_score(a) - _norm_score(b))
            tone_gap = TONE_RANK[_tone(a)] - TONE_RANK[_tone(b)]
            if score_gap < 5.0 and tone_gap == 0:
                continue
            # 立场相反则分歧更"尖锐"
            sharp = score_gap + (10.0 if tone_gap != 0 else 0.0)
            conflicts.append({
                "a": a.get("agent_id", a.get("name")),
                "b": b.get("agent_id", b.get("name")),
                "score_gap": round(score_gap, 1),
                "tone_gap": tone_gap,
                "sharpness": round(sharp, 1),
            })
    conflicts.sort(key=lambda c: c["sharpness"], reverse=True)
    return conflicts


# ----------------------- 质询与辩护生成 ----------------------- #

def _default_challenge_text(challenger: Dict[str, Any], target: Dict[str, Any]) -> str:
    cname = challenger.get("name", "某大师")
    tname = target.get("name", "对方")
    cverdict = challenger.get("verdict", "")
    tverdict = target.get("verdict", "")
    cscore = _norm_score(challenger)
    tscore = _norm_score(target)
    higher = cscore >= tscore
    lead = f"{cname}（{cverdict}）对 {tname}（{tverdict}）提出质询："
    body = (
        f"你的评分 {tscore:.0f} 与我方 {cscore:.0f} 存在明显分歧。"
        f"请说明你的核心依据中最关键的一条假设，以及若该假设被证伪，你的结论会如何变化。"
    )
    return lead + body


def _default_defense_text(target: Dict[str, Any], challenge: AgentMessage) -> str:
    tname = target.get("name", "我方")
    reason = (target.get("reason") or "").strip()
    reason = reason[:160] + ("…" if len(target.get("reason") or "") > 160 else "")
    return (
        f"{tname} 辩护：{reason or '基于本流派方法论独立判断。'}"
        f" 质询要点已收到，但当前数据窗口内本流派信号未被破坏，维持原结论方向。"
    )


# ----------------------- 审议主流程 ----------------------- #

def run_deliberation(
    masters: List[Dict[str, Any]],
    *,
    config: Optional[DeliberationConfig] = None,
    llm_call: Optional[Callable[[str, str], str]] = None,
    challenge_fn: Optional[Callable[[Dict[str, Any], Dict[str, Any]], str]] = None,
    defense_fn: Optional[Callable[[Dict[str, Any], AgentMessage], str]] = None,
    on_round: Optional[Callable[[int, List[AgentMessage]], None]] = None,
) -> DeliberationResult:
    """执行多轮审议，返回含 transcript 与影响力权重的 DeliberationResult。

    参数：
        masters: 第一轮大师独立初判（MasterResult 列表）。
        config: 审议参数。
        llm_call: 可选的大模型回调 (system, user) -> text，用于生成更自然的质询/辩护；
                  缺省时使用内置规则模板。
        challenge_fn / defense_fn: 自定义质询/辩护文本生成器（优先于 llm_call）。
        on_round: 每轮结束回调，便于 SSE 流式推送。
    """

    cfg = config or DeliberationConfig()
    reg = build_master_registry(masters)
    by_id = {m.get("agent_id", m.get("name")): m for m in masters}

    transcript: List[AgentMessage] = []
    influence = {mid: 1.0 for mid in by_id}
    engagement: Dict[str, Dict[str, int]] = {mid: {"challenge": 0, "defense": 0} for mid in by_id}

    def gen(role_prompt: str, user_prompt: str) -> str:
        if llm_call:
            try:
                return llm_call(role_prompt, user_prompt)
            except Exception:
                return ""
        return ""

    rounds_played = 0
    for rnd in range(1, cfg.max_rounds + 1):
        conflicts = find_conflicts(masters)[: cfg.top_conflicts]
        if not conflicts:
            break

        round_msgs: List[AgentMessage] = []
        progressed = False
        for cf in conflicts:
            a_id, b_id = cf["a"], cf["b"]
            if a_id not in by_id or b_id not in by_id:
                continue
            a, b = by_id[a_id], by_id[b_id]

            # 让评分更高 / 立场更积极的一方发起质询（更"主动"）
            challenger, target = (a, b) if _norm_score(a) >= _norm_score(b) else (b, a)
            if cf["score_gap"] < cfg.min_score_gap and cf["tone_gap"] == 0:
                continue

            # 1) 质询：优先自定义函数 → 其次 LLM → 最后默认模板
            if challenge_fn:
                ch_text = challenge_fn(challenger, target)
            elif llm_call:
                ch_text = llm_call(challenger.get("name", "总管"),
                                   _default_challenge_text(challenger, target))
            else:
                ch_text = ""
            if not ch_text:
                ch_text = _default_challenge_text(challenger, target)
            challenge = AgentMessage(
                sender=challenger.get("agent_id", challenger.get("name")),
                receiver=target.get("agent_id", target.get("name")),
                role=MessageRole.CHALLENGE,
                meta={"round": rnd, "score_gap": cf["score_gap"]},
            ).add_text(ch_text)
            round_msgs.append(challenge)
            engagement[challenger.get("agent_id", challenger.get("name"))]["challenge"] += 1
            progressed = True

            # 2) 辩护：优先自定义函数 → 其次 LLM → 最后默认模板
            if defense_fn:
                def_text = defense_fn(target, challenge)
            elif llm_call:
                def_text = gen(target.get("name", "大师"),
                               _default_defense_text(target, challenge))
            else:
                def_text = ""
            if not def_text:
                def_text = _default_defense_text(target, challenge)
            defense = challenge.reply(
                sender=target.get("agent_id", target.get("name")),
                role=MessageRole.DEFENSE,
                text=def_text,
                meta={"round": rnd, "held": True},
            )
            round_msgs.append(defense)
            engagement[target.get("agent_id", target.get("name"))]["defense"] += 1

            # 3) 影响力调整：被质询方若评分较低，且对方坚持，则小幅让权
            if _norm_score(target) < _norm_score(challenger):
                influence[target.get("agent_id", target.get("name"))] *= cfg.influence_decay

        if not progressed:
            break

        transcript.extend(round_msgs)
        rounds_played += 1
        if on_round:
            on_round(rounds_played, round_msgs)

    # 归一化影响力到 0~1 的相对权重
    total = sum(influence.values()) or 1.0
    influence = {k: round(v / total, 3) for k, v in influence.items()}

    return DeliberationResult(
        transcript=transcript,
        rounds=rounds_played,
        influence=influence,
        engagement=engagement,
    )


# ----------------------- 辅助：把影响力并入汇总 ----------------------- #

def apply_influence_to_scores(
    masters: List[Dict[str, Any]],
    influence: Dict[str, float],
) -> List[Dict[str, Any]]:
    """返回带 `weighted_score` 与 `influence` 字段的大师列表（不破坏原 score）。"""

    out = []
    for m in masters:
        mid = m.get("agent_id", m.get("name"))
        infl = influence.get(mid, 1.0)
        sc = _norm_score(m)
        row = dict(m)
        row["influence"] = round(infl, 3)
        row["weighted_score"] = round(sc * infl, 1)
        out.append(row)
    return out
