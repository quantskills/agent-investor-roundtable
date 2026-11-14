"""各投资大师 Agent。

每个 Agent 接收同一份共享数据包 context（由总管 Agent 派发），
按照各自投资体系独立研判，返回结构化投顾建议：
verdict / score / action / position / key_metrics / reason / watch_items。
"""
from __future__ import annotations

import math
from typing import Any


def _num(x) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _pct(v) -> str:
    x = _num(v)
    return "—" if x is None else f"{x * 100:.1f}%"


def _fmt(v, nd: int = 2) -> str:
    x = _num(v)
    return "—" if x is None else f"{x:.{nd}f}"


def _verdict(score: float, buy: float = 70, watch: float = 50) -> str:
    if score >= buy:
        return "买入"
    if score >= watch:
        return "观望"
    return "回避"


def _tone(score: float) -> str:
    return "up" if score >= 70 else ("mid" if score >= 50 else "down")


def _base_report(ctx: dict, agent: str, name: str, school: str, role: str) -> dict:
    return {
        "agent": agent,
        "name": name,
        "school": school,
        "role": role,
        "color": "#f0b90b",
        "score": 0,
        "verdict": "观望",
        "action": "等待更多数据",
        "position": 0.0,
        "key_metrics": [],
        "reason": "",
        "watch_items": [],
        "tone": "mid",
    }


def run_buffett(ctx: dict) -> dict:
    """沃伦·巴菲特：能力圈、高 ROE、护城河、低负债、安全边际。"""
    r = _base_report(ctx, "buffett", "沃伦·巴菲特", "价值投资", "只买看得懂的好公司，价格必须有安全边际")
    q = ctx.get("quality") or {}
    v = ctx.get("valuation") or {}
    m = ctx.get("management") or {}
    market = ctx.get("market") or {}
    combined = ctx.get("combined") or {}

    quality = _num(q.get("score")) or 0
    valuation = _num(v.get("score"))
    management = _num(m.get("score")) or 0
    market_score = _num(market.get("score"))
    total = _num(combined.get("total"))
    if total is None:
        total = quality * 0.40 + (valuation or 0) * 0.30 + management * 0.30
    mos = _num(v.get("margin_of_safety"))

    r["score"] = round(total, 1)
    r["tone"] = _tone(r["score"])
    r["verdict"] = _verdict(r["score"])
    if r["verdict"] == "买入" and (mos is None or mos < 0.10):
        r["verdict"] = "观望"

    pos = 0.0
    if r["verdict"] == "买入":
        pos = _clamp(0.08 + (r["score"] - 70) / 30 * 0.22 + (mos or 0) * 0.10, 0.10, 0.40)
    elif r["verdict"] == "观望":
        pos = _clamp((r["score"] - 50) / 20 * 0.08, 0.0, 0.08)
    r["position"] = round(pos, 2)

    r["key_metrics"] = [
        {"label": "企业质量", "value": f"{quality:.0f}"},
        {"label": "ROE", "value": _pct(q.get("roe_mean"))},
        {"label": "毛利率", "value": _pct(q.get("gross_margin"))},
        {"label": "负债/净资产", "value": _fmt(q.get("debt_eq"))},
        {"label": "安全边际", "value": _pct(mos)},
        {"label": "PE(TTM)", "value": _fmt(v.get("pe"))},
    ]
    r["reason"] = (
        f"质量 {quality:.0f}、管理层 {management:.0f}；安全边际 {_pct(mos)}，"
        f"{'满足好公司+好价格，可分批买入' if r['verdict'] == '买入' else '价格或质量尚不满足买入条件，保持耐心'}"
    )
    r["watch_items"] = [
        "ROE 是否持续高于 15%",
        "经营现金流/净利润是否长期大于 0.8",
        "负债率是否上升",
        f"价格是否回到买点 {_fmt(v.get('entry_price'))} 以内",
    ]
    if market_score is not None:
        r["watch_items"].append(f"市场情绪 {market_score:.0f} 分（低分时更贪婪）")
    return r


def run_lynch(ctx: dict) -> dict:
    """彼得·林奇：从生活与常识发现成长股，用 PEG 衡量性价比。"""
    r = _base_report(ctx, "lynch", "彼得·林奇", "成长股", "买能看得懂、成长快且 PEG 合理的公司")
    q = ctx.get("quality") or {}
    v = ctx.get("valuation") or {}
    industry = str(ctx.get("industry") or "")

    pe = _num(v.get("pe"))
    np_cagr = _num(q.get("np_cagr"))
    revenue_cagr = _num(q.get("revenue_cagr"))
    debt_eq = _num(q.get("debt_eq")) or 0
    dividend_yield = _num(v.get("dividend_yield")) or 0
    quality = _num(q.get("score")) or 0

    growth_pct = (np_cagr or 0) * 100
    peg = _safe_div(pe, max(growth_pct, 1.0))
    cyclical = any(k in industry for k in ("石油", "有色", "钢铁", "房地产", "汽车", "煤炭", "化工"))
    if growth_pct >= 20 and debt_eq < 1:
        stock_type = "快速成长股"
    elif growth_pct >= 10 and (pe or 99) < 25:
        stock_type = "稳定成长股"
    elif cyclical:
        stock_type = "周期股"
    elif growth_pct < 0:
        stock_type = "困境反转候选"
    elif dividend_yield >= 0.03 and (pe or 99) < 20:
        stock_type = "稳健收益股"
    else:
        stock_type = "普通成长股"

    score = 0.0
    score += _clamp((growth_pct - 5) * 1.2, 0, 30)
    if peg is not None:
        score += 40 if peg <= 1 else (28 if peg <= 1.5 else (12 if peg <= 2 else 2))
    else:
        score += 18
    score += quality * 0.30
    score = round(_clamp(score, 0, 100), 1)

    r["score"] = score
    r["tone"] = _tone(score)
    r["verdict"] = "买入" if (stock_type == "快速成长股" and (peg or 9) < 1) else (
        "观望" if (peg is None or peg < 1.8) else "回避"
    )
    pos = _clamp(0.10 + (1 - (peg or 1)) * 0.20 + score / 100 * 0.10, 0.0, 0.35)
    r["position"] = round(pos, 2)

    r["key_metrics"] = [
        {"label": "股票类型", "value": stock_type},
        {"label": "PE", "value": _fmt(pe)},
        {"label": "净利增速", "value": _pct(np_cagr)},
        {"label": "PEG", "value": _fmt(peg)},
        {"label": "营收增速", "value": _pct(revenue_cagr)},
        {"label": "股息率", "value": _pct(dividend_yield)},
    ]
    r["reason"] = (
        f"归类为「{stock_type}」，PEG {_fmt(peg)}；"
        f"{'高成长+合理估值，符合林奇买入标准' if r['verdict'] == '买入' else '成长与价格组合尚不理想，继续跟踪'}"
    )
    r["watch_items"] = [
        "未来 2-3 年净利润增速能否维持",
        "PEG 是否回到 1 附近",
        "库存与渠道变化是否损害成长逻辑",
        "有没有机构不断加仓验证逻辑",
    ]
    return r


def run_fisher(ctx: dict) -> dict:
    """菲利普·费雪：十五要点简化版，只买持续成长、研发强、管理层优秀的公司。"""
    r = _base_report(ctx, "fisher", "菲利普·费雪", "成长股", "买入真正出色的公司并长期持有")
    q = ctx.get("quality") or {}
    v = ctx.get("valuation") or {}
    m = ctx.get("management") or {}

    revenue_cagr = _num(q.get("revenue_cagr")) or 0
    np_cagr = _num(q.get("np_cagr")) or 0
    rd_ratio = _num(q.get("rd_ratio"))
    gross_margin = _num(q.get("gross_margin")) or 0
    roe = _num(q.get("roe_mean")) or 0
    ocf_ratio = _num(q.get("ocf_ratio_avg")) or 0
    management = _num(m.get("score")) or 0

    growth = _clamp((revenue_cagr + np_cagr) / 2 * 100 / 20 * 30, 0, 30)
    rd = 0 if rd_ratio is None else _clamp(rd_ratio / 0.08 * 20, 0, 20)
    margin = _clamp((gross_margin - 0.15) / 0.35 * 15, 0, 15)
    roe_score = _clamp((roe - 0.10) / 0.15 * 15, 0, 15)
    cash = _clamp(ocf_ratio / 1.0 * 10, 0, 10)
    score = round(_clamp(growth + rd + margin + roe_score + cash + management * 0.10, 0, 100), 1)

    r["score"] = score
    r["tone"] = _tone(score)
    r["verdict"] = "买入" if score >= 75 else ("观望" if score >= 60 else "回避")
    pos = _clamp((score - 55) / 45 * 0.30, 0.0, 0.30)
    r["position"] = round(pos, 2)

    r["key_metrics"] = [
        {"label": "营收CAGR", "value": _pct(revenue_cagr)},
        {"label": "研发占比", "value": _pct(rd_ratio)},
        {"label": "毛利率", "value": _pct(gross_margin)},
        {"label": "ROE", "value": _pct(roe)},
        {"label": "现金流/净利", "value": _fmt(ocf_ratio)},
        {"label": "管理层", "value": f"{management:.0f}"},
    ]
    r["reason"] = (
        f"费雪要点评分 {score:.0f}：成长 {growth:.0f} / 研发 {rd:.0f} / 毛利 {margin:.0f} / ROE {roe_score:.0f}；"
        f"{'具备长期成长股的稀缺特征，可集中持有' if r['verdict'] == '买入' else '某些要点未达标，暂不重仓'}"
    )
    r["watch_items"] = [
        "产品线能否持续带来 10 年以上增长",
        "研发投入是否持续转化为利润",
        "管理层是否言行一致、善待股东",
        "毛利率是否被竞争侵蚀",
    ]
    return r


def run_livermore(ctx: dict) -> dict:
    """杰西·利弗莫尔：只做趋势明确、量价配合的股票，破位无条件离场。"""
    r = _base_report(ctx, "livermore", "杰西·利弗莫尔", "趋势交易", "跟随最小阻力线，做右侧交易")
    tech = ctx.get("technical") or {}
    market = ctx.get("market") or {}

    close = _num(tech.get("close")) or _num(ctx.get("price"))
    ma5 = _num((tech.get("ma") or {}).get("MA5"))
    ma20 = _num((tech.get("ma") or {}).get("MA20"))
    ma60 = _num((tech.get("ma") or {}).get("MA60"))
    rsi = _num((tech.get("rsi") or {}).get("RSI12"))
    dif = _num((tech.get("macd") or {}).get("DIF"))
    dea = _num((tech.get("macd") or {}).get("DEA"))
    hist = _num((tech.get("macd") or {}).get("HIST"))
    change_pct = _num(tech.get("change_pct"))
    market_score = _num(market.get("score"))

    trend = 0
    if ma5 and ma20 and ma60:
        trend = 40 if (ma5 > ma20 > ma60) else (25 if (ma20 > ma60) else 8)
    elif ma20:
        trend = 22 if close and close > ma20 else 10
    momentum = 0
    if hist is not None:
        momentum += 15 if hist > 0 else -5
    if dif is not None and dea is not None:
        momentum += 10 if dif > dea else -5
    if rsi is not None:
        momentum += 10 if 50 <= rsi <= 70 else (5 if 30 <= rsi < 50 else (-5 if rsi > 80 else 0))
    if change_pct is not None:
        momentum += 8 if change_pct > 0 else -8
    market_part = ((market_score or 50) - 50) / 100 * 20
    score = round(_clamp(trend + momentum + market_part, 0, 100), 1)

    r["score"] = score
    r["tone"] = _tone(score)
    r["verdict"] = "买入" if score >= 70 else ("观望" if score >= 50 else "回避")
    pos = _clamp((score - 55) / 45 * 0.30, 0.0, 0.30)
    r["position"] = round(pos, 2)

    r["key_metrics"] = [
        {"label": "现价", "value": _fmt(close)},
        {"label": "MA20", "value": _fmt(ma20)},
        {"label": "MA60", "value": _fmt(ma60)},
        {"label": "RSI12", "value": _fmt(rsi, 1)},
        {"label": "MACD", "value": _fmt(hist)},
        {"label": "20日涨跌", "value": _pct(change_pct)},
    ]
    r["reason"] = (
        f"趋势分 {trend:.0f} / 动量分 {momentum:.0f} / 市场分 {market_part:.0f}；"
        f"{'趋势与量价共振，可右侧跟随' if r['verdict'] == '买入' else '方向未明，等待关键价位突破'}"
    )
    r["watch_items"] = [
        "是否放量突破近 3 个月高点",
        "价格是否站稳 MA20 / MA60",
        f"止损参考：{_fmt(ma20)} 或买入价 -8%",
        "大盘是否进入系统性下行",
    ]
    return r


def run_graham(ctx: dict) -> dict:
    """本杰明·格雷厄姆：防御型价值，低 PE/PB、高股息、低负债。"""
    r = _base_report(ctx, "graham", "本杰明·格雷厄姆", "防御价值", "用数据而非故事保护本金")
    v = ctx.get("valuation") or {}
    q = ctx.get("quality") or {}

    pe = _num(v.get("pe"))
    pb = _num(v.get("pb"))
    dividend_yield = _num(v.get("dividend_yield")) or 0
    debt_eq = _num(q.get("debt_eq")) or 9
    price = _num(v.get("price")) or _num(ctx.get("price"))

    eps = _safe_div(price, pe)
    bvps = _safe_div(price, pb)
    graham_value = None
    if eps is not None and bvps is not None and eps > 0 and bvps > 0:
        graham_value = math.sqrt(22.5 * eps * bvps)

    score = 0.0
    score += 35 if (pe or 99) <= 12 else (26 if (pe or 99) <= 15 else (14 if (pe or 99) <= 20 else 3))
    score += 25 if (pb or 99) <= 1.0 else (18 if (pb or 99) <= 1.5 else (9 if (pb or 99) <= 2.5 else 2))
    score += 25 if dividend_yield >= 0.04 else (17 if dividend_yield >= 0.03 else (9 if dividend_yield >= 0.02 else 2))
    score += 15 if debt_eq <= 0.5 else (10 if debt_eq <= 1 else 3)
    score = round(_clamp(score, 0, 100), 1)

    r["score"] = score
    r["tone"] = _tone(score)
    r["verdict"] = "买入" if score >= 70 else ("观望" if score >= 50 else "回避")
    pos = _clamp((score - 55) / 45 * 0.35, 0.0, 0.35)
    r["position"] = round(pos, 2)

    r["key_metrics"] = [
        {"label": "PE", "value": _fmt(pe)},
        {"label": "PB", "value": _fmt(pb)},
        {"label": "股息率", "value": _pct(dividend_yield)},
        {"label": "负债/净资产", "value": _fmt(debt_eq)},
        {"label": "格雷厄姆估值", "value": _fmt(graham_value)},
        {"label": "现价", "value": _fmt(price)},
    ]
    r["reason"] = (
        f"防御价值评分 {score:.0f}；格雷厄姆估值 {_fmt(graham_value)}，"
        f"{'价格处于防御区间，可配置' if r['verdict'] == '买入' else '估值安全垫不足，宁可错过不可买贵'}"
    )
    r["watch_items"] = [
        "PE 是否回到 15 倍以内",
        "PB 是否回到 1.5 倍以内",
        "股息是否连续 10 年稳定",
        "负债率是否显著抬升",
    ]
    return r


def _safe_div(a, b) -> float | None:
    a, b = _num(a), _num(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


MASTERS = [
    ("buffett", run_buffett, "沃伦·巴菲特", "价值投资"),
    ("lynch", run_lynch, "彼得·林奇", "成长股"),
    ("fisher", run_fisher, "菲利普·费雪", "成长股质量"),
    ("livermore", run_livermore, "杰西·利弗莫尔", "趋势交易"),
    ("graham", run_graham, "本杰明·格雷厄姆", "防御价值"),
]
