# -*- coding: utf-8 -*-
"""
游资 Agent 引擎桥接层
=====================
把 E:\游资agent集合 下 21 个游资量化 Agent（独立回测引擎）桥接为
「逐标的信号评估器」，供 A2A 层调用。

职责：
  * 动态加载 21 个 agent_*.py，提取 NAME / STYLE / STRATEGY / RISK_PARAMS；
  * 为每只游资按其心法风格实现轻量「逐标的评估」(evaluate)，输入个股
    K 线 + 市场上下文，输出投票 (verdict / conviction / note / risk_note)；
  * 通过项目既有数据层 app.data.pandadata_client 取真实 K 线（凭证仅来自
    环境变量，与全局要求一致）。

说明：原始 agent 文件是完整回测引擎（SimMarket / RealMarket），此处不改动
它们，而是额外提供「单标的快速评估」能力，使其能像 A2A 节点一样对某一
只股票发表观点。
"""

from __future__ import annotations

import os
import glob
import importlib.util
from dataclasses import dataclass, field
from typing import Optional

# 游资 Agent 代码目录（可通过环境变量覆盖）
YOUZI_AGENT_DIR = os.getenv("YOUZI_AGENT_DIR", r"E:\游资agent集合")


@dataclass
class YouziMeta:
    name: str
    style: str
    strategy: str
    risk: dict
    module_path: str


@dataclass
class YouziVote:
    name: str
    school: str = "游资实战"
    verdict: str = "观望"          # 买入 / 持有 / 卖出 / 观望
    conviction: float = 0.0        # 0~1 置信度
    note: str = ""
    risk_note: str = ""
    weight: float = 1.0            # 由风控参数推导的影响力


# ---------------------------------------------------------------------------
# 1. 加载 21 个游资 Agent 元信息
# ---------------------------------------------------------------------------
def _load_metas() -> list[YouziMeta]:
    metas: list[YouziMeta] = []
    if not os.path.isdir(YOUZI_AGENT_DIR):
        return metas
    for path in sorted(glob.glob(os.path.join(YOUZI_AGENT_DIR, "agent_*.py"))):
        try:
            spec = importlib.util.spec_from_file_location("youzi_" + os.path.basename(path)[:-3], path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            name = getattr(mod, "NAME", os.path.basename(path))
            style = getattr(mod, "STYLE", "")
            strategy = getattr(mod, "STRATEGY", "")
            risk = getattr(mod, "RISK_PARAMS", {}) or {}
            metas.append(YouziMeta(name=name, style=style, strategy=strategy,
                                   risk=risk, module_path=path))
        except Exception:
            # 单个 agent 加载失败不影响整体
            continue
    return metas


METAS = _load_metas()


# ---------------------------------------------------------------------------
# 2. K 线特征提取（基于项目数据层返回的 DataFrame）
# ---------------------------------------------------------------------------
def _features(kline) -> Optional[dict]:
    """从 K 线 DataFrame 计算统一技术特征。kline 需含 open/close/high/low/volume。"""
    if kline is None or getattr(kline, "empty", True) or len(kline) < 6:
        return None
    close = kline["close"].astype(float).tolist()
    vol = kline["volume"].astype(float).tolist()
    n = len(close)
    ma5 = sum(close[-5:]) / 5
    ma10 = sum(close[-10:]) / 10 if n >= 10 else ma5
    ma20 = sum(close[-20:]) / 20 if n >= 20 else ma5
    avgv = sum(vol) / n
    vol_ratio = (vol[-1] / avgv) if avgv else 1.0
    ret_1 = (close[-1] - close[-2]) / close[-2] if n >= 2 else 0.0
    ret_5 = (close[-1] - close[-6]) / close[-6] if n >= 6 else 0.0
    # 近期涨停数
    limit_ups = 0
    for i in range(max(0, n - 10), n):
        if i >= 1 and abs(close[i] - close[i - 1]) / close[i - 1] >= 0.095:
            limit_ups += 1
    # 最大回撤
    peak = max(close[-20:]) if n >= 20 else max(close)
    dd = (peak - close[-1]) / peak if peak else 0.0
    return {
        "close": close[-1], "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "avgv": avgv, "vol_ratio": vol_ratio, "ret_1": ret_1, "ret_5": ret_5,
        "limit_ups_10": limit_ups, "drawdown": dd,
        "uptrend": close[-1] > ma5 > ma10,
    }


# ---------------------------------------------------------------------------
# 3. 各游资风格评估器（按心法映射）
#    返回 (verdict, conviction, note)
# ---------------------------------------------------------------------------
def _eval_generic(meta: YouziMeta, f: dict) -> tuple[str, float, str]:
    """通用：趋势向上 + 放量 + 未过度追高 -> 买入，否则观望。"""
    if f["uptrend"] and f["vol_ratio"] > 1.2 and f["ret_5"] < 0.25:
        return "买入", 0.6, "趋势向上且放量，符合强势股参与条件"
    if f["uptrend"]:
        return "持有", 0.4, "维持上升趋势，继续观察"
    return "观望", 0.2, "趋势不足，空仓等待"


def _eval_limit_up(meta: YouziMeta, f: dict) -> tuple[str, float, str]:
    """涨停板/打板类（总舵主、赵老哥、令狐冲、小鳄鱼、著名刺客）：重势、看涨停。"""
    if f["limit_ups_10"] >= 1 and f["vol_ratio"] > 1.3:
        return "买入", 0.8, "近期有涨停且放量，打板/接力参与"
    if f["uptrend"] and f["vol_ratio"] > 1.5:
        return "买入", 0.6, "资金强势介入，跟随主流"
    if f["drawdown"] > 0.12:
        return "卖出", 0.5, "失势回落，离场"
    return "观望", 0.25, "未现涨停信号，等待"


def _eval_emotion(meta: YouziMeta, f: dict) -> tuple[str, float, str]:
    """情绪周期类（炒股养家、涅盘重升、龙飞虎、万法归宗）：情绪拐点/赢面。"""
    if f["ret_5"] > 0.03 and f["vol_ratio"] > 1.2:
        return "买入", 0.7, "情绪由冷转暖，赢面较高"
    if f["ret_5"] < -0.03:
        return "卖出", 0.5, "情绪转冷，降低仓位"
    return "持有", 0.4, "情绪中性，持有观察"


def _eval_trend_follow(meta: YouziMeta, f: dict) -> tuple[str, float, str]:
    """趋势跟踪类（不动明王、瑞鹤仙、丁一熊、浓汤野人）：5日线生命线。"""
    if f["close"] > f["ma5"] and f["vol_ratio"] > 1.1:
        return "买入", 0.65, "站上5日线量能健康，做多"
    if f["close"] < f["ma5"] * 0.98:
        return "卖出", 0.6, "有效跌破5日线，离场"
    return "持有", 0.4, "沿5日线震荡，持有"


def _eval_main_rise(meta: YouziMeta, f: dict) -> tuple[str, float, str]:
    """主升浪/龙头类（作手新一、章盟主、Asking、职业炒手）：只做主升/龙头。"""
    if f["uptrend"] and f["ret_5"] > 0.05 and f["vol_ratio"] > 1.2:
        return "买入", 0.85, "主升浪确立，龙头跟随"
    if f["limit_ups_10"] >= 2:
        return "买入", 0.8, "连板龙头，格局持有"
    if f["drawdown"] > 0.1:
        return "卖出", 0.55, "龙头失势，离场"
    return "观望", 0.3, "非主升段，不参与"


# 名称关键词 -> 评估器
_EVAL_MAP = [
    (("总舵主", "赵老哥", "令狐冲", "小鳄鱼", "著名刺客", "涨停"), _eval_limit_up),
    (("炒股养家", "涅盘", "龙飞虎", "万法归宗", "情绪"), _eval_emotion),
    (("不动明王", "瑞鹤仙", "丁一熊", "浓汤野人", "5日线", "趋势"), _eval_trend_follow),
    (("作手新一", "章盟主", "Asking", "职业炒手", "龙头", "主升"), _eval_main_rise),
]


def _pick_evaluator(meta: YouziMeta):
    for keys, fn in _EVAL_MAP:
        if any(k in meta.name for k in keys):
            return fn
    return _eval_generic


def evaluate(meta: YouziMeta, symbol: str, kline) -> YouziVote:
    """对单只股票评估某游资观点。"""
    f = _features(kline)
    if f is None:
        return YouziVote(name=meta.name, verdict="观望", conviction=0.0,
                         note="数据不足，无法评估")
    fn = _pick_evaluator(meta)
    verdict, conv, note = fn(meta, f)
    # 风控提示来自该游资参数
    rp = meta.risk
    risk_note = ""
    if rp.get("max_drawdown_stop"):
        risk_note = f"单标的最大回撤容忍 {rp.get('max_drawdown_stop'):.0%}"
    weight = float(rp.get("max_position", 0.6))
    return YouziVote(name=meta.name, verdict=verdict, conviction=conv,
                     note=note, risk_note=risk_note, weight=weight)


# ---------------------------------------------------------------------------
# 4. 对外：评估全部游资对某标的的观点
# ---------------------------------------------------------------------------
def evaluate_all(symbol: str, kline) -> list[YouziVote]:
    return [evaluate(m, symbol, kline) for m in METAS]


def list_metas() -> list[YouziMeta]:
    return METAS
