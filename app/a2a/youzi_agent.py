# -*- coding: utf-8 -*-
"""youzi_agent：把单个游资量化 Agent 封装为 A2A 网络中的 Agent 节点。

每个 YouziAgentNode 包装一位游资（来自 engine_bridge 加载的
BaseQuantAgent 子类），对外提供三件事：

1. AgentCard           -- A2A 能力发现名片（注册进 AgentRegistry）；
2. evaluate()          -- 把该游资在目标标的上的最新信号翻译成与
                          大师委员会完全兼容的 MasterResult 结构
                          （score/verdict/position/key_metrics/...）；
3. opinion()/defend()  -- 以 A2A 消息（OPINION / DEFENSE）参与多轮审议，
                          可直接被 deliberation.run_deliberation 复用。

评分映射（规则可解释，无 LLM 依赖）：
- BUY  信号：score = 62 + 36×权重 - 3×信号龄，仓位 0.10~0.40；
- SELL 信号：score = 28（回避），仓位 0；
- 无信号  ：score = 50 ± 风格抖动（观望），仓位 0。
回测画像（engine_bridge.get_backtest_stats）会作为 key_metrics 展示，
让"信号"与"该游资的历史战绩"同时可见。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from .protocol import AgentCard, AgentMessage, MessageRole

COMMITTEE_ID = "youzi_committee"


def _style_jitter(name: str, symbol: str) -> int:
    """确定性风格抖动 ±3 分：同一游资对同一标的恒定，避免同分扎堆。"""
    h = hashlib.md5(f"{name}|{symbol}".encode("utf-8")).hexdigest()
    return int(h[:4], 16) % 7 - 3


class YouziAgentNode:
    """单个游资的 A2A 节点。"""

    def __init__(
        self,
        agent_meta: Dict[str, Any],
        symbol_signals: Dict[str, Dict[str, Any]],
        symbol: str,
        market_ctx: Optional[Dict[str, Any]] = None,
        backtest_stats: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        参数：
            agent_meta: engine_bridge.load_youzi_agents() 里的一项
                        （name/style/strategy/agent_id/risk_params/...）
            symbol_signals: 该游资在目标标的上的信号
                        {"action","weight","reason","days_ago","history"}
            symbol: 目标标的（panda 代码）
            market_ctx: 标的最新快照 {close,pct_chg,date}
            backtest_stats: 该游资的回测画像（可空）
        """
        self.meta = agent_meta
        self.signals = symbol_signals or {"action": "HOLD", "weight": 0.0,
                                          "reason": "", "days_ago": None,
                                          "history": []}
        self.symbol = symbol
        self.market = market_ctx or {}
        self.stats = backtest_stats or {}
        self.card = AgentCard(
            agent_id=agent_meta["agent_id"],
            name=agent_meta["name"],
            school=f"游资·{agent_meta['style']}" if agent_meta["style"] else "游资",
            tone="mid",
            capabilities=["超短信号", "仓位权重", "逐标的评估"],
            description=agent_meta["strategy"][:80],
            endpoint="app.a2a.youzi_agent.YouziAgentNode.evaluate",
        )
        # evaluate() 的缓存结果
        self._result: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # MasterResult 兼容评估
    # ------------------------------------------------------------------

    def evaluate(self) -> Dict[str, Any]:
        """输出与大师委员会 MasterResult 同构的评估结果。"""
        if self._result is not None:
            return self._result

        name = self.meta["name"]
        action = str(self.signals.get("action", "HOLD")).upper()
        weight = float(self.signals.get("weight", 0) or 0)
        days_ago = self.signals.get("days_ago")
        age = int(days_ago) if days_ago is not None else 99
        reason = str(self.signals.get("reason", "") or "")
        jitter = _style_jitter(name, self.symbol)

        if action == "BUY":
            score = 62.0 + 36.0 * min(1.0, max(0.0, weight)) - 3.0 * age
            score = max(55.0, min(98.0, score))
            tone = "up"
            verdict = "买入" if score >= 72 else "观望"
            position = round(0.10 + 0.30 * min(1.0, weight), 2)
            action_txt = f"买入（建议仓位 {position:.0%}）" if verdict == "买入" else "试仓观察"
        elif action == "SELL":
            score = 28.0
            tone = "down"
            verdict, position, action_txt = "回避", 0.0, "清仓离场"
        else:
            score = 50.0 + jitter
            tone = "mid"
            verdict, position, action_txt = "观望", 0.0, "空仓等待时机"

        # ---- key_metrics：信号 + 回测画像 + 风控 ----
        key_metrics: List[Dict[str, str]] = [
            {"label": "最新信号", "value": f"{action}｜权重 {weight:.2f}｜{age} 日前"},
            {"label": "标的现价", "value": f"{self.market.get('close', '-')}（{self.market.get('pct_chg', 0):+.2f}%）"},
        ]
        if self.stats and "total_return" in self.stats:
            key_metrics.append({"label": "2024回测",
                                "value": f"收益 {self.stats['total_return']:+.1f}%｜胜率 {self.stats['win_rate']:.0f}%｜回撤 {self.stats['max_drawdown']:.1f}%"})
        rp = self.meta.get("risk_params") or {}
        for k in ("stop_loss", "take_profit", "position_limit"):
            if k in rp:
                key_metrics.append({"label": k.replace("_", " "),
                                    "value": f"{float(rp[k]):.0%}" if isinstance(rp[k], (int, float)) else str(rp[k])})

        # ---- reason / watch_items ----
        sig_txt = reason if reason else ("无触发信号，处于观察区" if action == "HOLD" else "风控离场")
        reason_full = (
            f"【{self.meta['style']}】{self.meta['strategy'][:48]}… "
            f"标的信号：{sig_txt[:80]}"
        )
        watch_items: List[str] = []
        if verdict == "买入":
            watch_items += ["次日竞价强弱（高开低走则减半仓）",
                            "板块梯队是否完整（龙头/跟风分化）"]
        if action == "SELL":
            watch_items.append("离场后观察 2 日，确认趋势破坏有效性")
        if action == "HOLD":
            watch_items.append("等待打板/低吸条件触发再介入")

        self._result = {
            "agent": self.card.agent_id,
            "agent_id": self.card.agent_id,
            "name": name,
            "school": self.card.school,
            "role": f"超短实战·{self.meta['style']}",
            "color": "#e91e63",
            "score": round(score, 1),
            "verdict": verdict,
            "action": action_txt,
            "position": position,
            "key_metrics": key_metrics,
            "reason": reason_full,
            "watch_items": watch_items,
            "tone": tone,
            "capabilities": self.card.capabilities,
        }
        return self._result

    # ------------------------------------------------------------------
    # A2A 消息（OPINION / DEFENSE）
    # ------------------------------------------------------------------

    def opinion(self) -> AgentMessage:
        """第一轮独立观点消息（广播给委员会）。"""
        r = self.evaluate()
        msg = AgentMessage(
            sender=self.card.agent_id,
            receiver=COMMITTEE_ID,
            role=MessageRole.OPINION,
            meta={"symbol": self.symbol, "score": r["score"],
                  "verdict": r["verdict"], "position": r["position"]},
        )
        msg.add_text(f"[{self.card.name}] 对 {self.symbol} 判定「{r['verdict']}」"
                     f"（{r['score']:.0f} 分）：{r['reason']}")
        msg.add_data({"signals": self.signals.get("history", []),
                      "action": self.signals.get("action"),
                      "weight": self.signals.get("weight")})
        return msg

    def defend(self, challenge: AgentMessage) -> AgentMessage:
        """回应质询：游资以自己的历史战绩与信号规则作答（规则式，无 LLM）。"""
        r = self.evaluate()
        stats_txt = ""
        if self.stats and "win_rate" in self.stats:
            stats_txt = (f" 本人体系 2024 年真实行情回测胜率 {self.stats['win_rate']:.0f}%、"
                         f"最大回撤 {self.stats['max_drawdown']:.1f}%。")
        reply = challenge.reply(
            sender=self.card.agent_id,
            role=MessageRole.DEFENSE,
            text=(f"[{self.card.name}] 辩护：本信号由「{self.meta['style']}」规则触发"
                  f"（{self.signals.get('reason', '') or '风控条件'}），"
                  f"并非主观判断。{stats_txt} 在信号被反向条件破坏前维持原结论。"),
            meta={"held": True, "symbol": self.symbol},
        )
        return reply
