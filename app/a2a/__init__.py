"""A2A（Agent-to-Agent）投顾委员会模块。

暴露协调器、大师 Agent、审议引擎与通信协议，供主程序 / API / CLI 调用。
"""

from .orchestrator import run_committee, VALID_ACTIONS
from .deliberation import (
    DeliberationConfig,
    DeliberationResult,
    run_deliberation,
    find_conflicts,
    apply_influence_to_scores,
)
from .protocol import (
    AgentCard,
    AgentMessage,
    AgentRegistry,
    MessageRole,
    Part,
    build_master_registry,
)

__all__ = [
    "run_committee",
    "VALID_ACTIONS",
    "DeliberationConfig",
    "DeliberationResult",
    "run_deliberation",
    "find_conflicts",
    "apply_influence_to_scores",
    "AgentCard",
    "AgentMessage",
    "AgentRegistry",
    "MessageRole",
    "Part",
    "build_master_registry",
]
