# -*- coding: utf-8 -*-
"""
游资委员会主任（A2A 协调者）
============================
职责：
  * 作为 A2A 网络中的一个协调节点，向 21 个游资 Agent 节点广播 consult
    消息（真正的 A2A 多对多协作），收集各自观点；
  * 汇总投票，形成「游资共识」；
  * 以 BaseStockMaster 身份注册进现有 Orchestrator，使总览管家在原有 5 位
    大师之外，额外获得游资短线/打板/情绪维度，且不改动任何原有玩法。

通信方式：复用全局 A2A Message 协议（JSON-RPC 风格）。
"""

from __future__ import annotations

from collections import Counter

from app.a2a.protocol import BaseAgent, Message, AgentRegistry
from app.a2a.masters import BaseStockMaster, MasterResult
from app.a2a.youzi import engine_bridge as eb
from app.data import pandadata_client as pdc


class YouziCommittee(BaseStockMaster, BaseAgent):
    """游资委员会主任：协调 21 个游资节点并向 Orchestrator 输出 MasterResult。"""

    school = "游资实战"
    tone = "激进敏锐"
    color = "#ff4d4f"
    influence = 1.1

    def __init__(self, registry: AgentRegistry = None, agent_id: str = "youzi.committee"):
        BaseAgent.__init__(self, agent_id)
        self.registry = registry
        self.capabilities = ["游资情绪共识", "打板/接力信号", "短线风控提示", "多Agent协同"]

    # ---- A2A：向各游资节点广播并收集观点 ----
    def consult_youzis(self, symbol: str, kline) -> list[dict]:
        votes: list[dict] = []
        if self.registry is None:
            # 退化模式：直接调用桥接层
            for v in eb.evaluate_all(symbol, kline):
                votes.append({
                    "name": v.name, "verdict": v.verdict, "conviction": v.conviction,
                    "note": v.note, "risk_note": v.risk_note, "weight": v.weight,
                })
            return votes

        targets = [a for a in self.registry.all() if a.agent_id.startswith("youzi.") and a.agent_id != self.agent_id]
        for agent in targets:
            msg = Message(sender=self.agent_id, receiver=agent.agent_id,
                          method="consult", params={"symbol": symbol, "kline": kline})
            try:
                resp = agent.handle(msg)
                votes.append(resp.params or {})
            except Exception:
                continue
        return votes

    # ---- 汇总为游资共识 ----
    def _aggregate(self, symbol: str, name: str, votes: list[dict]) -> MasterResult:
        if not votes:
            return MasterResult(
                agent_id=self.agent_id, name=self.name, school=self.school, tone=self.tone,
                capabilities=self.capabilities, score=50, verdict="观望", action="观望",
                position=0.0, key_metrics={"游资样本": "0/21"},
                reason="暂无可用的游资观点（可能为数据/网络原因）。", watch_items=[],
                color=self.color, influence=self.influence,
            )

        counts = Counter(v.get("verdict", "观望") for v in votes)
        total = len(votes)

        # 加权看多/看空分数
        bull = sum(v.get("conviction", 0) * v.get("weight", 1)
                   for v in votes if v.get("verdict") == "买入")
        bear = sum(v.get("conviction", 0) * v.get("weight", 1)
                   for v in votes if v.get("verdict") == "卖出")
        hold = sum(v.get("conviction", 0) * v.get("weight", 1)
                   for v in votes if v.get("verdict") == "持有")
        net = bull - bear

        if net > 0.6 and counts.get("买入", 0) >= total * 0.3:
            verdict, action = "买入", "买入"
        elif bear > bull and counts.get("卖出", 0) >= total * 0.3:
            verdict, action = "卖出", "卖出"
        elif hold >= bull and hold >= bear:
            verdict, action = "持有", "持有"
        else:
            verdict, action = "观望", "观望"

        score = int(50 + min(45, net / max(0.01, total) * 45))
        position = round(min(0.9, bull / max(0.01, total)) * 100, 1)

        avg_conv = sum(v.get("conviction", 0) for v in votes) / total
        # 最强看多游资作为 watch_items
        bulls = sorted([v for v in votes if v.get("verdict") == "买入"],
                       key=lambda x: x.get("conviction", 0), reverse=True)[:3]
        watch_items = [f"{b.get('name')}：{b.get('note','')}" for b in bulls]

        reason = (f"21 位游资中共 {counts.get('买入',0)} 看多、"
                  f"{counts.get('卖出',0)} 看空、{counts.get('持有',0)} 持有、"
                  f"{counts.get('观望',0)} 观望；加权看多 {bull:.2f}/看空 {bear:.2f}。"
                  f"游资共识偏「{verdict}」，平均置信 {avg_conv:.0%}。")

        return MasterResult(
            agent_id=self.agent_id, name=self.name, school=self.school, tone=self.tone,
            capabilities=self.capabilities, score=score, verdict=verdict, action=action,
            position=position,
            key_metrics={
                "游资看多": f"{counts.get('买入',0)}/{total}",
                "游资看空": f"{counts.get('卖出',0)}/{total}",
                "加权看多": f"{bull:.2f}",
                "平均置信": f"{avg_conv:.0%}",
            },
            reason=reason, watch_items=watch_items,
            color=self.color, influence=self.influence,
        )

    # ---- BaseStockMaster 接口：供 Orchestrator 调用 ----
    def deliberate(self, ctx: dict) -> MasterResult:
        symbol = ctx.get("symbol", "")
        name = ctx.get("name", symbol)
        try:
            kline = pdc.get_daily_kline(symbol, days=60)
        except Exception as e:
            kline = None
        votes = self.consult_youzis(symbol, kline)
        return self._aggregate(symbol, name, votes)

    # ---- 便于独立调试 ----
    def run_task(self, task: dict) -> Message:
        symbol = task.get("symbol", "")
        try:
            kline = pdc.get_daily_kline(symbol, days=60)
        except Exception:
            kline = None
        votes = self.consult_youzis(symbol, kline)
        return Message(sender=self.agent_id, receiver=task.get("sender", "orchestrator"),
                        method="youzi_consensus", params={"symbol": symbol, "votes": votes})
