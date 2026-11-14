"""FastAPI 应用：首页 + SSE 流式分析接口 + 静态资源。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.agent.core import run_agent
from app.config import get_settings
from app.schemas import AnalyzeRequest, LLMConfigRequest
from app.a2a import orchestrator as committee_tool
from app.tools import buffett as buffett_tool
from app.tools import technical as technical_tool

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="量化大富翁 · 巴菲特价值投资 Agent")


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "已配置"
    return f"{key[:4]}***{key[-4:]}"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> StreamingResponse:
    """SSE 流式返回：Agent 事件（status/chart/token/error/done）。"""

    def event_stream():
        for event in run_agent(req.query, req.question):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/kline")
def kline(symbol: str):
    """单独获取某标的的技术图表数据（供图表刷新，可选）。"""
    res = technical_tool.analyze(symbol)
    if res["ok"]:
        return {"ok": True, "data": res["data"]["chart"]}
    return {"ok": False, "error": res["error"]}


@app.get("/api/buffett/analyze")
def buffett_analyze(query: str):
    """直接返回单股巴菲特价值投资分析（JSON）。"""
    return buffett_tool.full_analysis(query)


@app.get("/api/buffett/screen")
def buffett_screen(index: str = "000300", top_n: int = 20, master: str = "buffett"):
    """按大师流派筛选股票池。master: buffett/lynch/fisher/livermore/graham。"""
    if master == "buffett":
        return buffett_tool.screen(index=index, top_n=top_n)
    return buffett_tool.screen_by_master(master, index=index, top_n=top_n)


@app.get("/api/buffett/screen_all")
def buffett_screen_all(index: str = "000300", top_n: int = 10):
    """五位大师各自筛选并汇总，返回每位大师 top 列表与共同选中的交集。"""
    return buffett_tool.screen_all(index=index, top_n=top_n)


@app.get("/api/a2a/committee")
def a2a_committee(query: str):
    """直接返回 A2A 投顾委员会报告（总管 + 五位大师 Agent，JSON）。"""
    return committee_tool.run_committee(query)


@app.get("/api/a2a/debate")
def a2a_debate(query: str, question: str = "", no_deliberation: bool = False, rounds: int = 2):
    """直接返回 A2A 多轮审议详情（大师 Agent 的质询/辩护记录、影响力权重、攻防强度）。

    返回字段：
        agents / agents_weighted：初判与加权后大师列表
        debate：辩论 transcript（每条含 sender/receiver/role/text/meta）
        influence：各 Agent 最终影响力权重
        engagement：各 Agent 质询/辩护次数
        debate_rounds：审议轮数
        conflicts / allocation / agent_scores：与委员会接口兼容
    """
    from app.a2a.deliberation import DeliberationConfig

    return committee_tool.run_committee(
        query,
        question=question,
        enable_deliberation=not no_deliberation,
        deliberation_config=DeliberationConfig(max_rounds=max(1, min(rounds, 5))),
    )


@app.get("/api/config/llm")
def get_llm_config():
    """返回当前大模型配置（Key 只显示掩码）。"""
    s = get_settings()
    return {
        "ok": True,
        "configured": s.is_llm_configured,
        "base_url": s.base_url,
        "model": s.model,
        "masked_key": _mask_key(s.api_key),
    }


@app.post("/api/config/llm")
def set_llm_config(req: LLMConfigRequest):
    """保存大模型 Key / Base URL / 模型，立即生效并写回 .env。"""
    s = get_settings()
    s.update_llm(req.api_key, req.base_url, req.model)
    return {
        "ok": True,
        "configured": s.is_llm_configured,
        "base_url": s.base_url,
        "model": s.model,
        "masked_key": _mask_key(s.api_key),
    }


# --------------------------------------------------------------------------- #
# 游资委员会（A2A·超短派）与治理调度 —— 新增路由，不影响以上任何既有接口 #
# --------------------------------------------------------------------------- #

@app.get("/api/a2a/youzi")
def a2a_youzi(query: str, question: str = "", no_deliberation: bool = False,
              signal_days: int = 0):
    """游资委员会：21 位游资 Agent 对标的做逐标的信号评估 + 多轮审议（JSON）。

    query 支持名称或 6 位代码；no_deliberation=true 跳过审议（更快）；
    signal_days>0 自定义信号聚合天数（默认 5）。
    """
    from app.a2a.youzi_committee import run_youzi_committee

    return run_youzi_committee(
        query,
        question=question,
        enable_deliberation=not no_deliberation,
        signal_days=signal_days or None,
    )


@app.get("/api/a2a/youzi/agents")
def a2a_youzi_agents():
    """游资委员会能力发现：21 位游资的 AgentCard 注册表。"""
    from app.a2a import engine_bridge

    try:
        cards = engine_bridge.youzi_registry_cards()
        return {"ok": True,
                "count": len(cards),
                "agents": [c.to_dict() for c in cards]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@app.get("/api/a2a/youzi/signals")
def a2a_youzi_signals(symbols: str, signal_days: int = 0):
    """逐标的信号矩阵：21 位游资 × 多标的（不进审议，供筛选）。

    symbols：逗号分隔的代码列表，如 600519.SH,000001.SZ。
    """
    from app.a2a.youzi_committee import run_youzi_screen

    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        return {"ok": False, "error": "symbols 为空"}
    return run_youzi_screen(sym_list, signal_days=signal_days or None)


@app.get("/api/a2a/council")
async def a2a_council(query: str, question: str = "", no_deliberation: bool = False):
    """双院合议：价值大师委员会 × 游资委员会，50/50 融合终裁（JSON）。"""
    return await committee_tool.run_council(
        query,
        question=question,
        enable_deliberation=not no_deliberation,
    )


@app.get("/api/a2a/registry")
def a2a_registry():
    """A2A 委员会联合注册表（价值大师 + 游资）。"""
    return committee_tool.get_committee_registry()


@app.get("/api/a2a/youzi/schedule")
def a2a_youzi_schedule_status():
    """游资治理调度器状态（任务列表/计划时间/最近快照）。"""
    from app.a2a.scheduler import get_scheduler

    return {"ok": True, "data": get_scheduler().status()}


@app.post("/api/a2a/youzi/schedule")
def a2a_youzi_schedule_control(req: dict):
    """治理调度器控制：{"action": "start|stop|status|run_now", "task": 任务名}。"""
    from app.a2a.scheduler import get_scheduler

    action = str(req.get("action", "status")).lower()
    sch = get_scheduler()
    if action == "start":
        return {"ok": True, "data": sch.start()}
    if action == "stop":
        return {"ok": True, "data": sch.stop()}
    if action == "run_now":
        task = str(req.get("task", ""))
        return sch.run_now(task)
    return {"ok": True, "data": sch.status()}


@app.get("/api/a2a/youzi/snapshots")
def a2a_youzi_snapshots(limit: int = 20):
    """最近 N 条治理周期快照（盘前扫描/盘后复盘/周度治理）。"""
    from app.a2a.scheduler import get_scheduler

    return {"ok": True, "data": get_scheduler().snapshots(limit)}


# 静态资源挂载（放在路由之后，避免覆盖 API）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
