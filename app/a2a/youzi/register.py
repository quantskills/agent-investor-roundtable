# -*- coding: utf-8 -*-
"""
游资 A2A 注册与集成
==================
把 21 个游资节点 + 游资委员会主任接入现有 A2A 网络与 Orchestrator。

集成原则：
  * 不修改原有 5 位大师（价值/成长/质量/红利/趋势）的任何逻辑；
  * 不修改大富翁核心玩法（game/player/move）；
  * 仅「附加」：注册游资节点、把游资委员会主任作为第 6 位大师加入委员会，
    使总览管家在原有一致意见之外，额外获得游资短线维度。
"""

from __future__ import annotations

from app.a2a.protocol import AgentRegistry
from app.a2a.orchestrator import Orchestrator
from app.a2a.youzi.youzi_agent import YouziAgent
from app.a2a.youzi.youzi_committee import YouziCommittee


def build_youzi_network(registry: AgentRegistry = None) -> tuple[AgentRegistry, YouziCommittee]:
    """创建/复用 registry，注册 21 个游资节点，并返回游资委员会主任。"""
    if registry is None:
        registry = AgentRegistry()

    # 避免重复注册
    existing = {a.agent_id for a in registry.all()}
    if not any(aid.startswith("youzi.") for aid in existing):
        YouziAgent.spawn_all(registry)

    committee = YouziCommittee(registry=registry, agent_id="youzi.committee")
    if "youzi.committee" not in existing:
        registry.register(committee)
    return registry, committee


def attach_to_orchestrator(orchestrator: Orchestrator,
                           registry: AgentRegistry = None) -> YouziCommittee:
    """把游资委员会主任作为第 6 位大师加入 Orchestrator。"""
    registry, committee = build_youzi_network(registry)
    # 去重：若已加入则跳过
    if not any(getattr(m, "agent_id", "") == "youzi.committee" for m in orchestrator.masters):
        orchestrator.add_master(committee)
    return committee
