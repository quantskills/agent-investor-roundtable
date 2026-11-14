"""A2A（Agent-to-Agent）通信协议层。

定义一套轻量但结构化的 Agent 间消息标准，灵感来自 Google A2A 协议的
Message / Part / AgentCard 概念，但针对本平台的投顾委员会场景做了裁剪：

- AgentCard：每个 Agent 的"名片"，用于能力发现与注册。
- Part：消息的最小内容单元（文本 / 数据 / 引用）。
- AgentMessage：一个 Agent 发给另一个 Agent（或广播给委员会）的消息。
- DebateMessage：审议流程中带语义角色的专用消息（质询 / 辩护 / 回应）。

所有消息都带唯一 id、时间戳与发送方，保证 Agent 间交互可追溯、可审计、
可被前端可视化还原成"辩论时间线"。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class MessageRole(str, Enum):
    """消息的语义角色。"""

    QUERY = "query"          # 总管向大师派发任务
    OPINION = "opinion"      # 大师独立观点（第一轮）
    CHALLENGE = "challenge"  # 质询：指出对方逻辑漏洞/数据冲突
    DEFENSE = "defense"      # 辩护：回应质询，可坚持或微调
    SYNTHESIS = "synthesis"  # 总管综合决议
    BROADCAST = "broadcast"  # 委员会广播（对所有 Agent）


class PartType(str, Enum):
    TEXT = "text"
    DATA = "data"
    REFERENCE = "reference"


@dataclass
class Part:
    """消息内容单元。一条消息可由多个 Part 组成。"""

    type: PartType
    content: Any
    mime_type: str = "text/plain"

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type.value, "content": self.content, "mime_type": self.mime_type}


@dataclass
class AgentCard:
    """Agent 名片：能力发现与注册元信息。"""

    agent_id: str
    name: str
    school: str           # 投资流派
    tone: str             # up / mid / down
    capabilities: List[str] = field(default_factory=list)
    description: str = ""
    endpoint: str = ""    # 逻辑端点标识（本平台为本地方法名）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentMessage:
    """Agent 间消息。"""

    sender: str                      # 发送方 agent_id
    receiver: str                    # 接收方 agent_id，"broadcast" 表示广播给委员会
    role: MessageRole
    parts: List[Part] = field(default_factory=list)
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:8]}")
    parent_id: Optional[str] = None  # 回复链的父消息 id
    created_at: float = field(default_factory=time.time)

    # 投票/质询等结构化负载，便于前端直接消费
    meta: Dict[str, Any] = field(default_factory=dict)

    def add_text(self, text: str) -> "AgentMessage":
        self.parts.append(Part(PartType.TEXT, text))
        return self

    def add_data(self, data: Any, mime: str = "application/json") -> "AgentMessage":
        self.parts.append(Part(PartType.DATA, data, mime))
        return self

    def text(self) -> str:
        return "\n".join(p.content for p in self.parts if p.type == PartType.TEXT)

    def reply(
        self,
        sender: str,
        role: MessageRole,
        text: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> "AgentMessage":
        """基于当前消息生成一条回复（自动关联 parent_id）。"""

        m = AgentMessage(
            sender=sender,
            receiver=self.sender,
            role=role,
            parent_id=self.message_id,
            meta=meta or {},
        )
        if text:
            m.add_text(text)
        return m

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "parent_id": self.parent_id,
            "sender": self.sender,
            "receiver": self.receiver,
            "role": self.role.value,
            "created_at": self.created_at,
            "meta": self.meta,
            "parts": [p.to_dict() for p in self.parts],
            "text": self.text(),
        }


class AgentRegistry:
    """Agent 名片注册表（能力发现）。"""

    def __init__(self) -> None:
        self._cards: Dict[str, AgentCard] = {}

    def register(self, card: AgentCard) -> None:
        self._cards[card.agent_id] = card

    def get(self, agent_id: str) -> Optional[AgentCard]:
        return self._cards.get(agent_id)

    def all(self) -> List[AgentCard]:
        return list(self._cards.values())

    def to_dict(self) -> List[Dict[str, Any]]:
        return [c.to_dict() for c in self._cards.values()]


def build_master_registry(masters: List[Dict[str, Any]]) -> AgentRegistry:
    """从 MasterResult 列表构建大师 Agent 注册表。"""

    reg = AgentRegistry()
    for m in masters:
        reg.register(
            AgentCard(
                agent_id=m.get("agent_id", m.get("name", "")),
                name=m.get("name", ""),
                school=m.get("school", ""),
                tone=m.get("tone", "mid"),
                capabilities=m.get("capabilities", []),
                description=m.get("school", ""),
            )
        )
    return reg


def serialize_messages(messages: List[AgentMessage]) -> str:
    return json.dumps([m.to_dict() for m in messages], ensure_ascii=False, indent=2)


def deserialize_messages(payload: str) -> List[AgentMessage]:
    raw = json.loads(payload)
    out: List[AgentMessage] = []
    for r in raw:
        msg = AgentMessage(
            sender=r["sender"],
            receiver=r["receiver"],
            role=MessageRole(r["role"]),
            message_id=r["message_id"],
            parent_id=r.get("parent_id"),
            created_at=r.get("created_at", time.time()),
            meta=r.get("meta", {}),
        )
        for p in r.get("parts", []):
            msg.parts.append(Part(PartType(p["type"]), p["content"], p.get("mime_type", "text/plain")))
        out.append(msg)
    return out
