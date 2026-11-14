# -*- coding: utf-8 -*-
"""engine_bridge：游资 Agent 引擎桥接层。

把 `E:\\游资agent集合` 下的 21 个游资量化 Agent（各自继承 BaseQuantAgent、
实现 generate_signals(bars, symbols, meta, day)）动态加载进量化大富翁进程，
并提供两件事：

1. load_youzi_agents()          —— 加载 21 个游资 Agent 类（懒加载 + 进程内缓存）
2. evaluate_signals(symbols)    —— 逐标的信号评估：对给定标的池，让每个游资
   Agent 在"最近 K 个交易日"的真实行情上跑 generate_signals，聚合出
   { 游资 -> 标的 -> 信号(BUY/SELL/HOLD, 权重, 理由, 信号龄) } 矩阵。

工程要点：
- 游资集合通过 sys.path 尾插 + sys.modules 预注册加载，不污染宿主模块空间；
- USE_REAL_DATA / PANDA_WATCHLIST / PANDA_START / PANDA_END 仅在评估期间
  临时设置（try/finally 恢复），凭证沿用宿主进程的 PANDA_USERNAME/PASSWORD；
- 行情拉取经由游资集合 pandadata_source 的模块级缓存，21 个 Agent 共享
  同一份日线数据，只发起一次 API 请求；
- 回测画像 get_backtest_stats() 会在标准窗口（2024 全年、6 标的、seed=7）
  上给每个游资跑一次回测，结果持久化到 youzi_cache/backtest_stats.json。
"""
from __future__ import annotations

import glob
import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .protocol import AgentCard

# ---------------------------------------------------------------------------
# 基础配置
# ---------------------------------------------------------------------------

_YOUZI_DIR = os.environ.get("YOUZI_AGENT_DIR", "E:/游资agent集合").strip()
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "youzi_cache")
_STATS_FILE = os.path.join(_CACHE_DIR, "backtest_stats.json")

# 信号评估：数据窗口（自然日）与信号聚合天数
_LOOKBACK_CALENDAR_DAYS = int(os.environ.get("YOUZI_LOOKBACK_DAYS", "300"))
_SIGNAL_DAYS = int(os.environ.get("YOUZI_SIGNAL_DAYS", "5"))

_LOAD_LOCK = threading.RLock()
_LOADED: Dict[str, dict] = {}          # agent_key -> 元信息（含类引用）
_STATS_LOCK = threading.RLock()
_STATS_CACHE: Optional[Dict[str, dict]] = None

# 标准回测画像窗口（与游资集合 run_real_agent.py all 的口径一致）
_BACKTEST_WINDOW = {
    "start": os.environ.get("YOUZI_BACKTEST_START", "20240101"),
    "end": os.environ.get("YOUZI_BACKTEST_END", "20241231"),
    "watchlist": [
        s.strip() for s in os.environ.get(
            "YOUZI_BACKTEST_WATCHLIST",
            "000001.SZ,600519.SH,300750.SZ,002594.SZ,601318.SH,000858.SZ",
        ).split(",") if s.strip()
    ],
}


# ---------------------------------------------------------------------------
# Agent 加载
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    """中文名 -> URL/ID 安全的 ascii slug（可逆性不重要，唯一即可）。"""
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:10]


def _preload_youzi_modules(youzi_dir: str) -> None:
    """把游资集合的基础模块按其原生模块名注册进 sys.modules。

    仅当宿主进程尚未占用同名模块时才注册（append 到 sys.path 尾部，
    绝不遮蔽宿主自己的包）。
    """
    for mod_name in ("config", "pandadata_source", "quant_agent_base"):
        if mod_name in sys.modules:
            continue
        path = os.path.join(youzi_dir, mod_name + ".py")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"游资集合缺少基础模块：{path}")
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod          # 先注册再 exec，处理相互 import
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise
    if youzi_dir not in sys.path:
        sys.path.append(youzi_dir)


def _find_agent_class(module) -> Optional[type]:
    """从已加载模块中找出 BaseQuantAgent 的具体子类。"""
    base = sys.modules.get("quant_agent_base")
    for attr in vars(module).values():
        if (isinstance(attr, type)
                and getattr(attr, "NAME", "")
                and base is not None
                and issubclass(attr, getattr(base, "BaseQuantAgent", object))
                and attr is not getattr(base, "BaseQuantAgent", None)):
            return attr
    # 兜底：任何带 NAME 属性且类名以 Agent 开头的类
    for attr in vars(module).values():
        if isinstance(attr, type) and getattr(attr, "NAME", "") and attr.__name__.startswith("Agent"):
            return attr
    return None


def load_youzi_agents(force: bool = False) -> Dict[str, dict]:
    """加载全部游资 Agent。

    返回 {agent_key: {"key","agent_id","name","style","strategy",
                      "risk_params","slug","cls","file"}}，进程内缓存。
    """
    with _LOAD_LOCK:
        if _LOADED and not force:
            return _LOADED
        if not os.path.isdir(_YOUZI_DIR):
            raise RuntimeError(
                f"游资 Agent 目录不存在：{_YOUZI_DIR}（可用环境变量 YOUZI_AGENT_DIR 指定）")
        _preload_youzi_modules(_YOUZI_DIR)

        files = sorted(glob.glob(os.path.join(_YOUZI_DIR, "agent_*.py")))
        if not files:
            raise RuntimeError(f"{_YOUZI_DIR} 下未找到 agent_*.py")

        loaded: Dict[str, dict] = {}
        for i, path in enumerate(files):
            mod_name = f"_youzi_mod_{i}_{os.path.basename(path)[:-3]}"
            spec = importlib.util.spec_from_file_location(mod_name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls = _find_agent_class(mod)
            if cls is None:
                continue
            name = str(getattr(cls, "NAME", os.path.basename(path)))
            style = str(getattr(cls, "STYLE", ""))
            strategy = str(getattr(cls, "STRATEGY", ""))
            risk = dict(getattr(cls, "RISK_PARAMS", {}) or {})
            loaded[name] = {
                "key": name,
                "agent_id": f"youzi_{_slugify(name)}",
                "name": name,
                "style": style,
                "strategy": strategy,
                "risk_params": risk,
                "slug": _slugify(name),
                "cls": cls,
                "file": os.path.basename(path),
            }
        if not loaded:
            raise RuntimeError("游资 Agent 加载结果为空")
        _LOADED.clear()
        _LOADED.update(loaded)
        return _LOADED


def youzi_registry_cards() -> List[AgentCard]:
    """21 位游资的 AgentCard 列表（A2A 能力发现）。"""
    agents = load_youzi_agents()
    cards = []
    for info in agents.values():
        cards.append(AgentCard(
            agent_id=info["agent_id"],
            name=info["name"],
            school=f"游资·{info['style']}" if info["style"] else "游资",
            tone="mid",
            capabilities=["超短信号", "仓位权重", "逐标的评估"],
            description=info["strategy"][:80],
            endpoint="app.a2a.engine_bridge.generate_signals",
        ))
    return cards


# ---------------------------------------------------------------------------
# 环境切换上下文（真实行情 + 目标窗口）
# ---------------------------------------------------------------------------

class _ScopedEnv:
    """评估期间临时覆盖游资集合读取的环境变量，退出时恢复。"""

    _KEYS = ("USE_REAL_DATA", "PANDA_WATCHLIST", "PANDA_START", "PANDA_END")

    def __init__(self, symbols: List[str], start: str, end: str):
        self._new = {
            "USE_REAL_DATA": "1",
            "PANDA_WATCHLIST": ",".join(symbols),
            "PANDA_START": start,
            "PANDA_END": end,
        }
        self._saved: Dict[str, Optional[str]] = {}

    def __enter__(self) -> "_ScopedEnv":
        for k in self._KEYS:
            self._saved[k] = os.environ.get(k)
            os.environ[k] = self._new[k]
        return self

    def __exit__(self, *exc) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# 逐标的信号评估
# ---------------------------------------------------------------------------

def _norm_sell_entry(entry) -> Tuple[str, str]:
    """sell_list 条目可能是 'SYM' 或 ('SYM', reason)，统一成 (sym, reason)。"""
    if isinstance(entry, (tuple, list)) and entry:
        sym = str(entry[0])
        reason = str(entry[1]) if len(entry) > 1 else ""
        return sym, reason
    return str(entry), ""


def _market_snapshot(bars: Dict[str, List[dict]],
                     meta: Optional[Dict[str, Any]] = None) -> Dict[str, dict]:
    """每个标的最新收盘/涨跌幅快照（给委员会做上下文）。"""
    dates = []
    if isinstance(meta, dict):
        dates = list(meta.get("dates") or [])
    snap: Dict[str, dict] = {}
    for sym, rows in bars.items():
        if not rows:
            continue
        last = rows[-1]
        idx = last.get("day")
        dstr = ""
        if isinstance(idx, int) and 0 <= idx < len(dates):
            dstr = str(dates[idx])
        snap[sym] = {
            "close": round(float(last.get("close", 0) or 0), 2),
            "pct_chg": round(float(last.get("ret", 0) or 0) * 100, 2),
            "date": dstr,
            "volume": float(last.get("vol", 0) or 0),
        }
    return snap


def evaluate_signals(
    symbols: List[str],
    signal_days: Optional[int] = None,
    capital: float = 1_000_000.0,
    seed: int = 42,
) -> Dict[str, Any]:
    """逐标的信号评估（真实行情）。

    对 `symbols` 中的每个标的，让全部游资 Agent 在最近
    `signal_days`（默认 5）个交易日上产生信号，按"最新信号优先 +
    信号龄衰减"聚合。

    返回：
    {
      "ok": True,
      "symbols": [...],
      "window": {"start","end","bars"},
      "signal_days": K,
      "market": {sym: {close,pct_chg,date}},
      "agents": {
          游资名: {
              "agent_id","style","strategy",
              "signals": {sym: {"action","weight","reason","days_ago","history"}},
          }
      },
      "consensus": {sym: {"buy","sell","hold","score","top_reason"}},
    }
    """
    if not symbols:
        raise ValueError("symbols 为空")
    signal_days = max(1, int(signal_days or _SIGNAL_DAYS))
    agents = load_youzi_agents()

    start = (date.today() - timedelta(days=_LOOKBACK_CALENDAR_DAYS)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")

    result_agents: Dict[str, dict] = {}
    bars_ref: Dict[str, List[dict]] = {}
    meta_ref: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    with _ScopedEnv(symbols, start, end):
        for name, info in agents.items():
            try:
                inst = info["cls"](capital=capital, seed=seed)
                bars, syms, meta = inst.market.generate()
                if not bars_ref:
                    bars_ref, meta_ref = bars, meta
                total_days = len(bars.get(symbols[0], []))
                if total_days == 0:
                    raise RuntimeError(f"{symbols[0]} 无行情数据")
                day_from = max(0, total_days - signal_days)

                agg: Dict[str, dict] = {s: {"action": "HOLD", "weight": 0.0,
                                            "reason": "", "days_ago": None,
                                            "history": []} for s in symbols}
                for day in range(day_from, total_days):
                    age = total_days - 1 - day          # 0 = 最新交易日
                    try:
                        buys, sells = inst.generate_signals(bars, syms, meta, day)
                    except Exception:
                        continue
                    for item in (buys or []):
                        if not isinstance(item, (tuple, list)) or len(item) < 2:
                            continue
                        sym = str(item[0])
                        if sym not in agg:
                            continue
                        w = max(0.0, min(1.0, float(item[1])))
                        reason = str(item[2]) if len(item) > 2 else ""
                        decay = 0.85 ** age
                        slot = agg[sym]
                        if slot["action"] != "BUY" or age < (slot["days_ago"] if slot["days_ago"] is not None else 99):
                            slot.update(action="BUY", weight=round(w * decay, 3),
                                        reason=reason, days_ago=age)
                        slot["history"].append({"day_offset": age, "action": "BUY",
                                                "weight": round(w, 3), "reason": reason})
                    for entry in (sells or []):
                        sym, reason = _norm_sell_entry(entry)
                        if sym not in agg:
                            continue
                        slot = agg[sym]
                        if slot["action"] != "SELL" or age < (slot["days_ago"] if slot["days_ago"] is not None else 99):
                            slot.update(action="SELL", weight=0.0,
                                        reason=reason or "风控/离场信号", days_ago=age)
                        slot["history"].append({"day_offset": age, "action": "SELL",
                                                "weight": 0.0, "reason": reason})
                for slot in agg.values():
                    slot["history"] = slot["history"][-6:]
                result_agents[name] = {
                    "agent_id": info["agent_id"],
                    "style": info["style"],
                    "strategy": info["strategy"],
                    "signals": agg,
                }
            except Exception as exc:  # 单个游资失败不影响整体
                errors[name] = f"{type(exc).__name__}: {exc}"

    if not result_agents:
        raise RuntimeError(f"所有游资 Agent 评估失败：{errors}")

    # RealMarket 的 meta 不含日期序列，快照日期回退为窗口结束日
    snap = _market_snapshot(bars_ref, meta_ref)
    for s in snap.values():
        if not s.get("date"):
            s["date"] = end

    # 共识统计（每标的）
    consensus: Dict[str, dict] = {}
    for sym in symbols:
        buys = [(n, a) for n, a in result_agents.items()
                if a["signals"][sym]["action"] == "BUY"]
        sells = [n for n, a in result_agents.items()
                 if a["signals"][sym]["action"] == "SELL"]
        wsum = sum(a["signals"][sym]["weight"] for _, a in buys)
        score = 50 + (50 * min(1.0, wsum / max(1, len(result_agents) * 0.35))
                      if buys else 0) - (12 * len(sells) / max(1, len(result_agents)))
        top = max(buys, key=lambda x: x[1]["signals"][sym]["weight"])[0] if buys else ""
        consensus[sym] = {
            "buy": len(buys), "sell": len(sells),
            "hold": len(result_agents) - len(buys) - len(sells),
            "score": round(max(0.0, min(100.0, score)), 1),
            "top_reason": (result_agents[top]["signals"][sym]["reason"][:60] if top else ""),
            "top_buyers": [n for n, _ in buys[:5]],
        }

    return {
        "ok": True,
        "symbols": symbols,
        "window": {"start": start, "end": end,
                   "bars": len(bars_ref.get(symbols[0], []))},
        "signal_days": signal_days,
        "evaluated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "market": snap,
        "agents": result_agents,
        "consensus": consensus,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 回测画像（懒加载 + JSON 持久化）
# ---------------------------------------------------------------------------

def get_backtest_stats(force: bool = False) -> Dict[str, dict]:
    """21 个游资在标准窗口上的回测画像 {游资名: {return,max_dd,win_rate,...}}。"""
    global _STATS_CACHE
    with _STATS_LOCK:
        if _STATS_CACHE and not force:
            return _STATS_CACHE
        if os.path.isfile(_STATS_FILE) and not force:
            try:
                with open(_STATS_FILE, encoding="utf-8") as f:
                    _STATS_CACHE = json.load(f)
                return _STATS_CACHE
            except Exception:
                pass

        agents = load_youzi_agents()
        stats: Dict[str, dict] = {}
        w = _BACKTEST_WINDOW
        with _ScopedEnv(w["watchlist"], w["start"], w["end"]):
            for name, info in agents.items():
                try:
                    inst = info["cls"](capital=1_000_000, seed=7)
                    rep = inst.run(verbose=False)
                    stats[name] = {
                        "total_return": round(float(rep.get("total_return", 0)) * 100, 2),
                        "max_drawdown": round(float(rep.get("max_drawdown", 0)) * 100, 2),
                        "win_rate": round(float(rep.get("win_rate", 0)) * 100, 1),
                        "trades": int(rep.get("trades", 0)),
                        "round_trips": int(rep.get("round_trips", 0)),
                        "final_equity": round(float(rep.get("final_equity", 0)), 0),
                        "window": f'{w["start"]}~{w["end"]}',
                    }
                except Exception as exc:
                    stats[name] = {"error": f"{type(exc).__name__}: {exc}",
                                   "window": f'{w["start"]}~{w["end"]}'}
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _STATS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _STATS_FILE)
        _STATS_CACHE = stats
        return stats


if __name__ == "__main__":
    cards = youzi_registry_cards()
    print(f"加载游资 Agent：{len(cards)} 位")
    for c in cards:
        print(f"  {c.agent_id}  {c.name}  {c.school}")
    res = evaluate_signals(["600519.SH"])
    print("\n贵州茅台 共识：", json.dumps(res["consensus"], ensure_ascii=False, indent=1))
