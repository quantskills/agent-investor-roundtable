# -*- coding: utf-8 -*-
"""
单个游资 Agent 的 A2A 节点封装
==============================
把 engine_bridge.YouziMeta 包装为符合 app.a2a.protocol.BaseAgent 协议的
A2A 节点。委员会（YouziCommittee）通过发送 consult 消息向其征询对某只
股票的观点，节点返回 YouziVote。

通信方式：JSON-RPC 风格 Message（与全局 A2A 协议一致）。
"""

from __future__ import annotations

from app.a2a.protocol import BaseAgent, Message, AgentRegistry
from app.a2a.youzi import engine_bridge as eb


class YouziAgent(BaseAgent):
    """一名游资的 A2A 节点。"""

    def __init__(self, meta: eb.YouziMeta, agent_id: str = None):
        super().__init__(agent_id or f"youzi.{meta.name}")
        self.meta = meta
        self.school = "游资实战"
        self.tone = "激进敏锐" if "涨停" in meta.style or "龙头" in meta.style else "顺势而为"
        self.capabilities = ["短线打板", "情绪判断", "强势股识别", "风控提示"]

    def handle(self, message: Message) -> Message:
        """处理咨询消息，返回观点。"""
        method = message.method
        payload = message.params or {}
        if method == "consult":
            symbol = payload.get("symbol", "")
            kline = payload.get("kline")
            vote = eb.evaluate(self.meta, symbol, kline)
            return Message(
                sender=self.agent_id,
                receiver=message.sender,
                method="consult_result",
                params={
                    "symbol": symbol,
                    "name": vote.name,
                    "school": vote.school,
                    "verdict": vote.verdict,
                    "conviction": vote.conviction,
                    "note": vote.note,
                    "risk_note": vote.risk_note,
                    "weight": vote.weight,
                    "style": self.meta.style,
                },
                trace_id=message.trace_id,
            )
        # 未知方法：透传（保持协议兼容）
        return Message(sender=self.agent_id, receiver=message.receiver,
                       method="error", params={"error": f"unsupported method: {method}"},
                       trace_id=message.trace_id)

    @staticmethod
    def spawn_all(registry: AgentRegistry):
        """构建 21 个游资节点并注册到 registry。"""
        agents = []
        for i, meta in enumerate(eb.list_metas()):
            a = YouziAgent(meta, agent_id=f"youzi.{i:02d}.{meta.name}")
            registry.register(a)
            agents.append(a)
        return agents
