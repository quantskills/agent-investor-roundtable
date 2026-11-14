# -*- coding: utf-8 -*-
"""scheduler：游资委员会的治理周期与任务调度机制。

以"治理周期（Governance Cycle）"为单位组织游资委员会的例行工作：

    盘前扫描   pre_market     交易日 09:00   跑一次逐标的信号筛选（run_youzi_screen），
                                      产出当日观察池与共识排名；
    盘后复盘   post_market    交易日 15:30   对核心标的跑完整游资委员会审议
                                      （run_youzi_committee），归档决议快照；
    周度治理   weekly_governance  周五 16:00 刷新 21 位游资的回测画像
                                      （engine_bridge.get_backtest_stats(force=True)），
                                      作为下周影响力与展示的底层数据。

实现要点：
- 单后台线程 + 停止事件，30s 粒度轮询，按"下一个计划时间"触发；
- 每个任务独立 try/except，失败记录进快照，不影响下一周期；
- 治理周期 ID 按自然日递增（G20260824-1、-2…），快照保留最近 50 条，
  并整体持久化到 youzi_cache/governance_snapshots.json，重启可续；
- 默认不自动启动：由 API（POST /api/a2a/youzi/schedule）或环境变量
  YOUZI_SCHEDULER=1 显式开启，不改变宿主应用的既有行为。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import Any, Callable, Deque, Dict, List, Optional

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "youzi_cache")
_SNAP_FILE = os.path.join(_CACHE_DIR, "governance_snapshots.json")

_TICK_SECONDS = 30.0
_MAX_SNAPSHOTS = 50

# 默认治理标的池（可用环境变量覆盖）
_DEFAULT_WATCHLIST = [
    s.strip() for s in os.environ.get(
        "YOUZI_SCHEDULER_WATCHLIST",
        "600519.SH,000001.SZ,300750.SZ,002594.SZ",
    ).split(",") if s.strip()
]
# 盘后复盘的核心标的（跑完整委员会审议）
_DEFAULT_FOCUS = os.environ.get("YOUZI_SCHEDULER_FOCUS", "600519.SH")


class _TaskSpec:
    """一个治理任务的静态描述。"""

    def __init__(self, name: str, label: str, hour: int, minute: int,
                 weekday: Optional[int], runner: Callable[[], Dict[str, Any]]) -> None:
        self.name = name
        self.label = label
        self.hour = hour
        self.minute = minute
        self.weekday = weekday            # None=每天；0=周一 … 4=周五
        self.runner = runner

    def next_run(self, after: datetime) -> datetime:
        """after 之后（含）该任务的下一个计划触发时刻。"""
        cand = after.replace(hour=self.hour, minute=self.minute,
                             second=0, microsecond=0)
        if cand <= after:
            cand += timedelta(days=1)
        while self.weekday is not None and cand.weekday() != self.weekday:
            cand += timedelta(days=1)
        return cand

    def due_today_window(self, now: datetime) -> bool:
        """盘前/盘后任务只在交易日触发（跳过周末；节假日由数据层自然兜底）。"""
        return now.weekday() < 5


class GovernanceScheduler:
    """游资委员会治理调度器（进程内单例，线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tasks: Dict[str, _TaskSpec] = {}
        self._next_run: Dict[str, datetime] = {}
        self._last_run: Dict[str, float] = {}
        self._last_status: Dict[str, str] = {}
        self._snapshots: Deque[Dict[str, Any]] = deque(maxlen=_MAX_SNAPSHOTS)
        self._cycle_counter = 0
        self._cycle_date = date.today()
        self._load_snapshots()
        self._register_default_tasks()

    # ---------------- 周期与快照 ---------------- #

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _new_cycle_id(self) -> str:
        with self._lock:
            today = date.today()
            if today != self._cycle_date:
                self._cycle_date = today
                self._cycle_counter = 0
            self._cycle_counter += 1
            return f"G{today.strftime('%Y%m%d')}-{self._cycle_counter}"

    def _load_snapshots(self) -> None:
        try:
            if os.path.isfile(_SNAP_FILE):
                with open(_SNAP_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                self._snapshots = deque(data.get("snapshots", [])[-_MAX_SNAPSHOTS:],
                                        maxlen=_MAX_SNAPSHOTS)
                self._cycle_date = date.today()
                self._cycle_counter = 0
        except Exception:
            pass

    def _persist_snapshots(self) -> None:
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            tmp = _SNAP_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "snapshots": list(self._snapshots)}, f,
                          ensure_ascii=False, indent=1)
            os.replace(tmp, _SNAP_FILE)
        except Exception:
            pass

    # ---------------- 任务注册 ---------------- #

    def _register_default_tasks(self) -> None:
        def _pre_market() -> Dict[str, Any]:
            from .youzi_committee import run_youzi_screen
            res = run_youzi_screen(_DEFAULT_WATCHLIST)
            data = res.get("data") or {}
            return {
                "kind": "screen",
                "watchlist": _DEFAULT_WATCHLIST,
                "ranking": data.get("ranking", [])[:5],
            }

        def _post_market() -> Dict[str, Any]:
            from .youzi_committee import run_youzi_committee
            res = run_youzi_committee(_DEFAULT_FOCUS, with_backtest_stats=False)
            if not res.get("ok"):
                return {"kind": "committee", "error": res.get("error", "")}
            data = res["data"]
            s = data["summary"]
            votes = {"买入": 0, "观望": 0, "回避": 0}
            for a in s.get("agents", []):
                v = a.get("verdict", "观望")
                votes[v if v in votes else "观望"] += 1
            return {
                "kind": "committee",
                "symbol": data["symbol"],
                "verdict": s["verdict"][:160],
                "score": s["score"],
                "position": s["allocation_position"],
                "votes": votes,
            }

        def _weekly() -> Dict[str, Any]:
            from . import engine_bridge
            stats = engine_bridge.get_backtest_stats(force=True)
            ok = sum(1 for v in stats.values() if "total_return" in v)
            return {"kind": "backtest_refresh", "agents": len(stats), "ok_agents": ok}

        self.register(_TaskSpec("pre_market", "盘前扫描", 9, 0, None, _pre_market))
        self.register(_TaskSpec("post_market", "盘后复盘", 15, 30, None, _post_market))
        self.register(_TaskSpec("weekly_governance", "周度治理", 16, 0, 4, _weekly))

    def register(self, spec: _TaskSpec) -> None:
        with self._lock:
            self._tasks[spec.name] = spec
            self._next_run[spec.name] = spec.next_run(datetime.now())

    # ---------------- 执行 ---------------- #

    def _execute(self, spec: _TaskSpec, trigger: str) -> Dict[str, Any]:
        snap: Dict[str, Any] = {
            "cycle_id": self._new_cycle_id(),
            "task": spec.name,
            "label": spec.label,
            "trigger": trigger,
            "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "ok",
            "result": {},
        }
        try:
            snap["result"] = spec.runner() or {}
            if isinstance(snap["result"], dict) and snap["result"].get("error"):
                snap["status"] = "error"
                snap["error"] = snap["result"]["error"]
        except Exception as exc:  # 单任务失败不阻塞调度
            snap["status"] = "error"
            snap["error"] = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._last_run[spec.name] = time.time()
            self._last_status[spec.name] = snap["status"]
            self._snapshots.append(snap)
            self._persist_snapshots()
        return snap

    def run_now(self, task: str) -> Dict[str, Any]:
        """立即执行一次指定任务（供 API 手动触发 / 测试）。"""
        with self._lock:
            spec = self._tasks.get(task)
        if not spec:
            return {"ok": False,
                    "error": f"未知任务 {task}，可选：{sorted(self._tasks)}"}
        return {"ok": True, "snapshot": self._execute(spec, "manual")}

    # ---------------- 线程循环 ---------------- #

    def _loop(self) -> None:
        while not self._stop.wait(_TICK_SECONDS):
            now = datetime.now()
            with self._lock:
                due = []
                for name, spec in self._tasks.items():
                    nr = self._next_run.get(name)
                    if nr is None:
                        self._next_run[name] = spec.next_run(now)
                        continue
                    if now >= nr:
                        due.append(spec)
                        self._next_run[name] = spec.next_run(now)
            for spec in due:
                if spec.weekday is None and not spec.due_today_window(now):
                    continue  # 周末不跑日频任务
                self._execute(spec, "schedule")

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self.running:
                return {"ok": True, "message": "already running"}
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="youzi-governance", daemon=True)
            self._thread.start()
        return {"ok": True, "message": "scheduler started"}

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            if not self.running:
                return {"ok": True, "message": "not running"}
            self._stop.set()
            thread = self._thread
        thread.join(timeout=_TICK_SECONDS + 5)
        return {"ok": True, "message": "scheduler stopped"}

    # ---------------- 状态查询 ---------------- #

    def status(self) -> Dict[str, Any]:
        with self._lock:
            tasks = []
            for name, spec in self._tasks.items():
                tasks.append({
                    "name": name,
                    "label": spec.label,
                    "schedule": ("周" + "一二三四五六日"[spec.weekday]
                                 if spec.weekday is not None else "每天")
                                + f" {spec.hour:02d}:{spec.minute:02d}",
                    "next_run": (self._next_run.get(name).strftime("%Y-%m-%d %H:%M")
                                 if self._next_run.get(name) else None),
                    "last_run": (time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(self._last_run[name]))
                                 if name in self._last_run else None),
                    "last_status": self._last_status.get(name),
                })
            return {
                "running": self.running,
                "tick_seconds": _TICK_SECONDS,
                "watchlist": _DEFAULT_WATCHLIST,
                "focus": _DEFAULT_FOCUS,
                "tasks": tasks,
                "cycles_today": self._cycle_counter,
                "snapshots": len(self._snapshots),
                "recent": list(self._snapshots)[-5:],
            }

    def snapshots(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._snapshots)[-max(1, min(limit, _MAX_SNAPSHOTS)):]


# ---------------- 进程内单例 ---------------- #

_INSTANCE: Optional[GovernanceScheduler] = None
_INSTANCE_LOCK = threading.Lock()


def get_scheduler() -> GovernanceScheduler:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = GovernanceScheduler()
        return _INSTANCE


def maybe_autostart() -> bool:
    """环境变量 YOUZI_SCHEDULER=1 时自动启动（默认关闭，不影响宿主）。"""
    if os.environ.get("YOUZI_SCHEDULER", "").strip().lower() in ("1", "true", "yes", "on"):
        get_scheduler().start()
        return True
    return False
