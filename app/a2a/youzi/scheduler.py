# -*- coding: utf-8 -*-
"""
游资 A2A 治理调度器
==================
任务调度机制：把「数据 → 分析 → 游资委员会/大师研讨 → 管家聚合 → 风控护栏
→ 交易执行」串成一条可重复的治理周期（Governance Cycle）。

调度层次：
  L1 数据刷新：经 app.data.pandadata_client 拉取/更新行情（带缓存）；
  L2 个体分析：full_analysis 构造 ctx；
  L3 多 Agent 研讨：Orchestrator 调度 5 位原大师 + 游资委员会主任，各自
     A2A 研讨后返回 MasterResult；
  L4 管家聚合：build_committee_report 形成一致意见；
  L5 风控护栏：对聚合后的 action/position 做硬性约束（回撤/仓位上限/黑名单）；
  L6 执行（可选）：把最终指令交给交易执行服务。

调度方式：支持单次 cycle() 与周期性 schedule()（后台线程，固定间隔刷新）。
"""

from __future__ import annotations

import time
import threading
from typing import Callable, Optional

from app.analysis import full_analysis
from app.a2a.orchestrator import Orchestrator
from app.a2a.deliberation import build_committee_report


# 默认风控护栏：可在实例化时覆盖
def _default_risk_guard(aggregate: dict) -> dict:
    action = aggregate.get("action", "观望")
    position = float(aggregate.get("position", 0.0) or 0.0)
    # 硬约束：单一标的仓位不超过 40%，且看空时不低于 0
    position = max(0.0, min(0.4, position))
    if action == "卖出":
        position = 0.0
    aggregate["position"] = position
    aggregate["risk_guard"] = "ok"
    return aggregate


class GovernanceScheduler:
    def __init__(self,
                 orchestrator: Orchestrator,
                 risk_guard: Callable[[dict], dict] = _default_risk_guard,
                 executor: Optional[Callable[[str, str, float], None]] = None):
        self.orchestrator = orchestrator
        self.risk_guard = risk_guard
        self.executor = executor
        self._timer = None
        self._stop = False

    def cycle(self, symbol: str, with_data_refresh: bool = True) -> dict:
        """跑一轮完整治理周期，返回最终决策 dict。"""
        # L1 数据：full_analysis 内部会触发数据层缓存刷新
        ctx = full_analysis(symbol)

        # L3 多 Agent 研讨（含游资委员会主任）
        results = self.orchestrator.run_full_analysis(symbol)

        # L4 管家聚合
        report = build_committee_report(results, ctx)
        aggregate = {
            "symbol": symbol,
            "name": ctx.get("name", symbol),
            "action": report.get("action", "观望"),
            "position": float(report.get("position", 0.0) or 0.0),
            "score": report.get("score", 50),
            "verdict": report.get("verdict", "观望"),
            "reason": report.get("reason", ""),
            "watch_items": report.get("watch_items", []),
            "panel": report.get("panel", []),
        }

        # L5 风控护栏
        aggregate = self.risk_guard(aggregate)

        # L6 执行（可选）
        if self.executor and aggregate.get("action") in ("买入", "卖出") and aggregate.get("position", 0) > 0:
            try:
                self.executor(aggregate["action"], symbol, aggregate["position"])
            except Exception:
                aggregate["exec_error"] = True

        aggregate["youzi_votes"] = [r for r in results
                                    if getattr(r, "agent_id", "") == "youzi.committee"]
        return aggregate

    def schedule(self, symbol: str, interval_sec: int = 3600):
        """后台周期调度（演示用，固定间隔重跑 cycle）。"""
        def _loop():
            while not self._stop:
                try:
                    self.cycle(symbol)
                except Exception:
                    pass
                time.sleep(interval_sec)

        self._stop = False
        self._timer = threading.Thread(target=_loop, daemon=True)
        self._timer.start()

    def stop(self):
        self._stop = True
        if self._timer:
            self._timer.join(timeout=1)
