# -*- coding: utf-8 -*-
"""游资 A2A 子包：把 21 个游资量化 Agent 接入量化大富翁投顾委员会。

组成：
  engine_bridge  游资引擎桥接（加载元信息 + 逐标的信号评估）
  youzi_agent    单个游资的 A2A 节点
  youzi_committee 游资委员会主任（协调 21 节点，输出 MasterResult）
  scheduler      治理调度器（数据→分析→研讨→聚合→风控→执行）
  register       注册/集成到现有 Orchestrator
"""

from app.a2a.youzi.engine_bridge import YouziMeta, YouziVote, evaluate, evaluate_all, list_metas
from app.a2a.youzi.youzi_agent import YouziAgent
from app.a2a.youzi.youzi_committee import YouziCommittee
from app.a2a.youzi.register import build_youzi_network, attach_to_orchestrator
from app.a2a.youzi.scheduler import GovernanceScheduler

__all__ = [
    "YouziMeta", "YouziVote", "evaluate", "evaluate_all", "list_metas",
    "YouziAgent", "YouziCommittee", "build_youzi_network", "attach_to_orchestrator",
    "GovernanceScheduler",
]
