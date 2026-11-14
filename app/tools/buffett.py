"""巴菲特价值投资分析引擎。

把巴菲特的核心思想工程化为可计算规则：
1. 能力圈 / 商业模式：高 ROE、稳定毛利、低负债、现金流与利润匹配
2. 管理层与股东回报：分红、回购、大股东行为、审计意见、股权结构
3. 安全边际：用 owner earnings 做保守 DCF，只在显著折价时出手
4. 市场情绪：指数估值百分位低时贪婪、高时谨慎
5. 集中持仓：只买看得懂的好公司，仓位与置信度、安全边际挂钩

工具统一返回 {"ok", "data": {"summary", "chart"}, "error"}。
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.data import pandadata_client as pdc


# --------------------------- 数值工具 --------------------------- #
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


def _safe_div(a, b) -> float | None:
    a, b = _num(a), _num(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


def _mean(values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _cagr(values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    if len(nums) < 2 or any(v <= 0 for v in nums):
        return None
    return (nums[-1] / nums[0]) ** (1 / (len(nums) - 1)) - 1


def _cv(values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    if len(nums) < 3:
        return None
    mean = sum(nums) / len(nums)
    if not mean:
        return None
    var = sum((v - mean) ** 2 for v in nums) / len(nums)
    return math.sqrt(var) / abs(mean)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _f2(x, suffix: str = "") -> str:
    v = _num(x)
    if v is None:
        return "—"
    return f"{v:.2f}{suffix}"


def _f1(x, suffix: str = "") -> str:
    v = _num(x)
    if v is None:
        return "—"
    return f"{v:.1f}{suffix}"


def _yi(x) -> str:
    v = _num(x)
    if v is None:
        return "—"
    return f"{v / 1e8:.2f} 亿"


def _pct(x, nd: int = 1) -> str:
    v = _num(x)
    if v is None:
        return "—"
    return f"{v * 100:.{nd}f}%"


def _is_financial(industry: str | None) -> bool:
    if not industry:
        return False
    return any(k in industry for k in ("银行", "非银金融", "保险", "证券"))


# --------------------------- 财务行指标 --------------------------- #
def _row_metrics(row: pd.Series) -> dict[str, Any]:
    revenue = _num(row.get("is_revenue"))
    np_attr = _num(row.get("is_n_income_attr_p"))
    ocf = _num(row.get("cfs_net_cash_operating"))
    assets = _num(row.get("bs_total_assets"))
    equity = _num(row.get("bs_total_hldr_eqy_exc_min_int")) or _num(row.get("bs_total_hldr_eqy_inc_min_int"))
    liab = _num(row.get("bs_total_liab"))
    shares = _num(row.get("bs_cap_stk"))
    eps = _num(row.get("is_basic_eps"))
    gross = _num(row.get("is_gross_profit"))
    rd = _num(row.get("is_rd_exp"))

    return {
        "revenue": revenue,
        "gross_profit": gross,
        "net_profit": np_attr,
        "ocf": ocf,
        "assets": assets,
        "equity": equity,
        "liab": liab,
        "shares": shares,
        "eps": eps,
        "rd": rd,
        "gross_margin": _safe_div(gross, revenue),
        "net_margin": _safe_div(np_attr, revenue),
        "roe": _safe_div(np_attr, equity),
        "ocf_ratio": _safe_div(ocf, np_attr),
        "debt_eq": _safe_div(liab, equity),
        "debt_ratio": _safe_div(liab, assets),
        "rd_ratio": _safe_div(rd, revenue),
    }


def _annuals_from_fina(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """从财务季度报告中提取年度（q4）指标，按披露日取最新修订。"""
    annuals: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        q = str(row.get("quarter") or "")
        if not q.endswith("q4"):
            continue
        annuals[q[:4]] = _row_metrics(row)
    return annuals


def _quarter_map(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        q = str(row.get("quarter") or "")
        if q:
            out[q] = _row_metrics(row)
    return out


def _ttm(quarter_map: dict[str, dict[str, Any]], annuals: dict[str, dict[str, Any]], key: str) -> float | None:
    """用累计报表计算 TTM：最新累计 + 上年全年 - 上年同累计。"""
    if not quarter_map:
        return None
    latest_q = max(quarter_map)
    year, qnum = int(latest_q[:4]), int(latest_q[-1])
    latest = _num(quarter_map[latest_q].get(key))
    if latest is None:
        return None
    if qnum == 4:
        return latest
    prev_annual = _num((annuals.get(str(year - 1)) or {}).get(key))
    prev_same = _num((quarter_map.get(f"{year - 1}q{qnum}") or {}).get(key))
    if prev_annual is None or prev_same is None:
        return latest
    return latest + prev_annual - prev_same


# --------------------------- 企业质量 / 护城河 --------------------------- #
def _quality_from_annuals(annuals: dict[str, dict[str, Any]], industry: str | None = None) -> dict:
    years = sorted(annuals)
    roes = [_num(annuals[y].get("roe")) for y in years]
    ocf_ratios = [_num(annuals[y].get("ocf_ratio")) for y in years]
    margins = [_num(annuals[y].get("gross_margin")) for y in years]
    net_margins = [_num(annuals[y].get("net_margin")) for y in years]
    debt_eqs = [_num(annuals[y].get("debt_eq")) for y in years]
    revenues = [_num(annuals[y].get("revenue")) for y in years]
    nps = [_num(annuals[y].get("net_profit")) for y in years]
    rd_ratios = [_num(annuals[y].get("rd_ratio")) for y in years]

    latest = annuals[years[-1]] if years else {}
    roe_latest = _num(latest.get("roe"))
    roe_mean = _mean(roes[-3:] if len(roes) >= 3 else roes)
    roe_cv = _cv(roes[-3:] if len(roes) >= 3 else roes)
    revenue_cagr = _cagr(revenues[-4:] if len(revenues) >= 4 else revenues)
    np_cagr = _cagr(nps[-4:] if len(nps) >= 4 else nps)
    ocf_ratio_avg = _mean(ocf_ratios[-3:] if len(ocf_ratios) >= 3 else ocf_ratios)
    debt_eq = _num(latest.get("debt_eq"))
    gross_margin = _mean(margins[-3:] if len(margins) >= 3 else margins)
    net_margin = _mean(net_margins[-3:] if len(net_margins) >= 3 else net_margins)
    rd_ratio = _mean(rd_ratios[-3:] if len(rd_ratios) >= 3 else rd_ratios)

    financial = _is_financial(industry)

    # 盈利能力 25 分
    if roe_latest is None:
        profit_score = 0
    elif roe_latest >= 0.20:
        profit_score = 25
    elif roe_latest >= 0.15:
        profit_score = 19
    elif roe_latest >= 0.10:
        profit_score = 11
    elif roe_latest >= 0.05:
        profit_score = 5
    else:
        profit_score = 0

    # 稳定性 10 分
    if roe_cv is None:
        stability_score = 5
    elif roe_cv <= 0.15:
        stability_score = 10
    elif roe_cv <= 0.30:
        stability_score = 7
    elif roe_cv <= 0.50:
        stability_score = 4
    else:
        stability_score = 0

    # 毛利率 15 分（金融业改用净息差代理，缺失给中性分）
    if financial:
        margin_score = 8 if net_margin is not None and net_margin >= 0.15 else 6
    elif gross_margin is None:
        margin_score = 4
    elif gross_margin >= 0.40:
        margin_score = 15
    elif gross_margin >= 0.30:
        margin_score = 11
    elif gross_margin >= 0.20:
        margin_score = 7
    else:
        margin_score = 2

    # 净利率 10 分
    if net_margin is None:
        net_score = 3
    elif net_margin >= 0.20:
        net_score = 10
    elif net_margin >= 0.10:
        net_score = 7
    elif net_margin >= 0.05:
        net_score = 4
    else:
        net_score = 1

    # 现金流 15 分
    if financial:
        cash_score = 8
    elif ocf_ratio_avg is None:
        cash_score = 4
    elif ocf_ratio_avg >= 0.8:
        cash_score = 15
    elif ocf_ratio_avg >= 0.5:
        cash_score = 10
    elif ocf_ratio_avg >= 0:
        cash_score = 5
    else:
        cash_score = 0

    # 财务稳健 15 分
    if financial:
        debt_score = 8
    elif debt_eq is None:
        debt_score = 5
    elif debt_eq <= 0.5:
        debt_score = 15
    elif debt_eq <= 1.0:
        debt_score = 11
    elif debt_eq <= 2.0:
        debt_score = 6
    else:
        debt_score = 0

    # 成长 10 分
    if np_cagr is None:
        growth_score = 3
    elif np_cagr >= 0.10:
        growth_score = 10
    elif np_cagr >= 0.05:
        growth_score = 7
    elif np_cagr >= 0:
        growth_score = 4
    else:
        growth_score = 0

    score = round(
        profit_score + stability_score + margin_score + net_score + cash_score + debt_score + growth_score,
        1,
    )

    signals: list[str] = []
    if roe_mean is not None:
        if roe_mean >= 0.15:
            signals.append(f"近 3 年平均 ROE {_pct(roe_mean)}，具备优秀商业模式特征")
        elif roe_mean >= 0.10:
            signals.append(f"近 3 年平均 ROE {_pct(roe_mean)}，盈利能力中等偏上")
        else:
            signals.append(f"近 3 年平均 ROE {_pct(roe_mean)}，盈利能力偏弱")
    if gross_margin is not None:
        if gross_margin >= 0.40:
            signals.append(f"平均毛利率 {_pct(gross_margin)}，定价权与护城河特征明显")
        elif gross_margin >= 0.20:
            signals.append(f"平均毛利率 {_pct(gross_margin)}，行业竞争力一般")
        else:
            signals.append(f"平均毛利率 {_pct(gross_margin)}，竞争激烈、溢价能力弱")
    if ocf_ratio_avg is not None:
        if ocf_ratio_avg >= 0.8:
            signals.append(f"经营现金流/净利润均值 {_f2(ocf_ratio_avg)}，利润含金量高")
        elif ocf_ratio_avg < 0.5:
            signals.append(f"经营现金流/净利润均值 {_f2(ocf_ratio_avg)}，利润与现金流背离，需警惕")
    if debt_eq is not None:
        if debt_eq <= 0.5:
            signals.append(f"负债/净资产 {_f2(debt_eq)}，财务非常保守")
        elif debt_eq > 2:
            signals.append(f"负债/净资产 {_f2(debt_eq)}，杠杆偏高")
    if np_cagr is not None:
        if np_cagr >= 0.10:
            signals.append(f"净利润年复合增速 {_pct(np_cagr)}，成长性与复利能力良好")
        elif np_cagr is not None and np_cagr < 0:
            signals.append(f"净利润年复合增速 {_pct(np_cagr)}，长期成长性不足")

    return {
        "years": years,
        "annuals": annuals,
        "roe_latest": roe_latest,
        "roe_mean": roe_mean,
        "roe_cv": roe_cv,
        "revenue_cagr": revenue_cagr,
        "np_cagr": np_cagr,
        "ocf_ratio_avg": ocf_ratio_avg,
        "debt_eq": debt_eq,
        "debt_ratio": _num(latest.get("debt_ratio")),
        "gross_margin": gross_margin,
        "net_margin": net_margin,
        "rd_ratio": rd_ratio,
        "score": score,
        "signals": signals,
        "is_financial": financial,
        "annual_table": [
            {
                "year": y,
                "revenue": annuals[y].get("revenue"),
                "net_profit": annuals[y].get("net_profit"),
                "roe": annuals[y].get("roe"),
                "gross_margin": annuals[y].get("gross_margin"),
                "net_margin": annuals[y].get("net_margin"),
                "ocf": annuals[y].get("ocf"),
                "ocf_ratio": annuals[y].get("ocf_ratio"),
                "debt_eq": annuals[y].get("debt_eq"),
                "eps": annuals[y].get("eps"),
            }
            for y in years
        ],
    }


def quality_analysis(symbol: str, industry: str | None = None) -> dict:
    fina = pdc.get_fina_reports(symbol, "2021q4", "2026q4")
    if fina.empty:
        return {"ok": False, "error": f"未获取到 {symbol} 的财务报告", "data": None}
    annuals = _annuals_from_fina(fina)
    if not annuals:
        return {"ok": False, "error": f"{symbol} 缺少年度财务数据", "data": None}
    q = _quality_from_annuals(annuals, industry)
    return {"ok": True, "data": q, "error": None}


# --------------------------- 估值 / 安全边际 --------------------------- #
def _valuation_score(pe: float | None, pb: float | None, yield_: float | None, mos: float | None, financial: bool) -> float:
    score = 0.0
    if pe is None:
        score += 10
    elif pe <= 12:
        score += 28
    elif pe <= 20:
        score += 22
    elif pe <= 30:
        score += 14
    elif pe <= 40:
        score += 8
    else:
        score += 2

    if pb is None:
        score += 12
    elif financial:
        score += 20 if pb <= 1.0 else (14 if pb <= 1.5 else (8 if pb <= 2.5 else 3))
    elif pb <= 1.5:
        score += 22
    elif pb <= 3:
        score += 15
    elif pb <= 5:
        score += 8
    else:
        score += 2

    if yield_ is None:
        score += 8
    elif yield_ >= 0.03:
        score += 20
    elif yield_ >= 0.02:
        score += 14
    elif yield_ >= 0.01:
        score += 8
    else:
        score += 2

    if mos is None:
        score += 15
    elif mos >= 0.30:
        score += 30
    elif mos >= 0.15:
        score += 22
    elif mos >= 0:
        score += 12
    else:
        score += 0
    return round(min(score, 100), 1)


def valuation_analysis(
    symbol: str,
    price: float,
    shares: float | None,
    quarter_map: dict[str, dict[str, Any]],
    annuals: dict[str, dict[str, Any]],
    dps_latest: float | None,
    quality: dict,
) -> dict:
    np_ttm = _ttm(quarter_map, annuals, "net_profit")
    ocf_ttm = _ttm(quarter_map, annuals, "ocf")
    equity = _num((annuals[max(annuals)] if annuals else {}).get("equity")) if annuals else None

    if np_ttm is None:
        np_ttm = _mean([annuals[y].get("net_profit") for y in sorted(annuals)[-3:]])
    if ocf_ttm is None:
        ocf_ttm = _mean([annuals[y].get("ocf") for y in sorted(annuals)[-3:]])

    owner_earnings = _mean([np_ttm, ocf_ttm]) if (np_ttm is not None and ocf_ttm is not None) else np_ttm
    if owner_earnings is None or owner_earnings <= 0:
        return {
            "owner_earnings": None,
            "np_ttm": np_ttm,
            "pe": None,
            "pb": None,
            "dividend_yield": dps_latest / price if (dps_latest and price) else None,
            "error": "无正的 owner earnings，无法可靠估值",
        }

    q_score = _num(quality.get("score")) or 50
    np_cagr = _num(quality.get("np_cagr"))
    if q_score >= 80:
        discount = 0.08
        base_g = _clamp(0.5 * (np_cagr or 0.06) + 0.04, 0.04, 0.10)
    elif q_score >= 60:
        discount = 0.09
        base_g = _clamp(0.5 * (np_cagr or 0.04) + 0.03, 0.03, 0.08)
    else:
        discount = 0.10
        base_g = _clamp(0.5 * (np_cagr or 0.02) + 0.02, 0.02, 0.06)

    bear_g = 0.02
    base_g = min(base_g, discount - 0.03)
    bull_g = min(base_g + 0.03, discount - 0.01)

    def gordon(g: float) -> float:
        return owner_earnings * (1 + g) / (discount - g)

    bear_value = gordon(bear_g)
    base_value = gordon(base_g)
    bull_value = gordon(bull_g)

    per_share = lambda v: (v / shares) if (shares and shares > 0) else None
    price_ps = price
    intrinsic = per_share(base_value)
    bear_ps, bull_ps = per_share(bear_value), per_share(bull_value)
    mos = _safe_div(intrinsic, price_ps) - 1 if (intrinsic and price_ps) else None

    eps_ttm = _safe_div(np_ttm, shares)
    bvps = _safe_div(equity, shares)
    pe = _safe_div(price_ps, eps_ttm)
    pb = _safe_div(price_ps, bvps)
    dividend_yield = _safe_div(dps_latest, price_ps)

    verdict = "明显低估（机会区）" if mos is not None and mos >= 0.30 else (
        "低估（可分批买入）" if mos is not None and mos >= 0.15 else (
            "合理区间（等待更好价格）" if mos is not None and mos >= 0 else (
                "偏高（注意风险）" if mos is not None else "数据不足"
            )
        )
    )

    signals = [
        f"owner earnings（保守估计）{_yi(owner_earnings)}",
        f"保守 DCF 内在价值 {_f2(intrinsic)} / 当前价 {_f2(price_ps)} / 安全边际 {_pct(mos) if mos is not None else '—'}",
        f"PE(TTM) {_f2(pe)} / PB {_f2(pb)} / 股息率 {_pct(dividend_yield) if dividend_yield is not None else '—'}",
        f"建议买点（内在价值 × 0.75）≈ {_f2(intrinsic * 0.75) if intrinsic else '—'}",
    ]

    score = _valuation_score(pe, pb, dividend_yield, mos, bool(quality.get("is_financial")))
    return {
        "owner_earnings": owner_earnings,
        "np_ttm": np_ttm,
        "ocf_ttm": ocf_ttm,
        "discount_rate": discount,
        "growth_base": base_g,
        "bear_value": bear_ps,
        "base_value": intrinsic,
        "bull_value": bull_ps,
        "price": price_ps,
        "margin_of_safety": mos,
        "pe": pe,
        "pb": pb,
        "dividend_yield": dividend_yield,
        "entry_price": intrinsic * 0.75 if intrinsic else None,
        "verdict": verdict,
        "score": score,
        "signals": signals,
    }


# --------------------------- 管理层 / 股东回报 --------------------------- #
def management_analysis(symbol: str, price: float | None, shares: float | None, detail: dict | None = None) -> dict:
    start = (date.today() - timedelta(days=365 * 8)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")

    dividends = pdc.get_cash_dividend(symbol, start, end)
    buybacks = pdc.get_repurchase(symbol, start, end)
    holders = pdc.get_top_holders(symbol, start, end, top_n=10)
    holder_count = pdc.get_holder_count(symbol, start, end)
    share_change = pdc.get_shareholder_change(symbol, start, end)
    pledge = pdc.get_pledge(symbol, start, end)
    audit = pdc.get_audit_opinion(symbol, "2021q1", "2025q4")

    # 分红
    div_by_year: dict[str, float] = {}
    div_rows = 0
    if not dividends.empty and {"div_cash_gross", "round_lot", "quarter"} <= set(dividends.columns):
        for _, row in dividends.iterrows():
            q = str(row.get("quarter") or "")
            dps = _safe_div(_num(row.get("div_cash_gross")), _num(row.get("round_lot")))
            if not q or dps is None:
                continue
            year = q[:4]
            div_by_year[year] = div_by_year.get(year, 0.0) + dps
            div_rows += 1
    div_years = sorted(div_by_year)
    dps_latest = div_by_year[div_years[-1]] if div_years else None
    div_growth = _cagr([div_by_year[y] for y in div_years[-5:]]) if div_years else None
    payout = None  # 由调用方结合最新净利润计算派息率

    # 回购
    buyback_rows: list[dict] = []
    buyback_total = 0.0
    if not buybacks.empty:
        for _, row in buybacks.iterrows():
            value = _num(row.get("buy_back_value")) or _num(row.get("value_ceiling")) or 0
            buyback_total += value
            buyback_rows.append({
                "date": str(row.get("announcement_dt") or row.get("date") or ""),
                "status": str(row.get("procedure") or ""),
                "value": value,
                "purpose": str(row.get("purpose") or "")[:80],
            })
        buyback_rows = buyback_rows[-3:]

    # 前十大股东集中度
    holder_conc = {"top1": None, "top5": None, "top10": None, "latest_date": None}
    if not holders.empty and "hold_percent_float" in holders.columns and "date" in holders.columns:
        h = holders.copy()
        h["date"] = h["date"].astype(str)
        latest_h = h[h["date"] == h["date"].max()]
        pcts = [_num(v) for v in latest_h["hold_percent_float"] if _num(v) is not None]
        if pcts:
            holder_conc = {
                "top1": pcts[0],
                "top5": sum(pcts[:5]),
                "top10": sum(pcts[:10]),
                "latest_date": str(h["date"].max()),
            }

    # 股东户数变化
    holder_trend = None
    if not holder_count.empty and "date" in holder_count.columns and "holders" in holder_count.columns:
        hc = holder_count.copy()
        hc["date"] = hc["date"].astype(str)
        hc = hc.sort_values("date")
        vals = [_num(v) for v in hc["holders"] if _num(v) is not None]
        if len(vals) >= 2:
            diff = (vals[-1] / vals[0] - 1) if vals[0] else None
            if diff is not None:
                holder_trend = {
                    "from": vals[0],
                    "to": vals[-1],
                    "change_pct": diff,
                    "text": "股东户数下降，筹码趋于集中" if diff < 0 else "股东户数上升，筹码趋于分散",
                }

    # 增减持
    buy_count = sell_count = 0
    latest_changes: list[dict] = []
    if not share_change.empty and "direction" in share_change.columns:
        for _, row in share_change.iterrows():
            d = str(row.get("direction") or "")
            if "增" in d:
                buy_count += 1
            elif "减" in d:
                sell_count += 1
            latest_changes.append({
                "date": str(row.get("info_date") or ""),
                "holder": str(row.get("shareholder_name") or "")[:30],
                "direction": d,
                "reason": str(row.get("reason") or "")[:60],
            })
        latest_changes = latest_changes[-4:]

    # 质押
    pledge_risk = not pledge.empty

    # 审计意见
    audit_ok = None
    audit_latest = None
    if not audit.empty and "opinion" in audit.columns:
        aud = audit.copy()
        if "date" in aud.columns:
            aud["date"] = aud["date"].astype(str)
            aud = aud.sort_values("date")
        annual_rows = aud[aud["quarter"].astype(str).str.endswith("q4")]
        audit_latest = annual_rows.iloc[-1] if not annual_rows.empty else aud.iloc[-1]
        opinion = str(audit_latest.get("opinion") or "")
        audit_ok = opinion == "unqualified_opinion"

    special_type = str((detail or {}).get("special_type") or "")
    special_risk = special_type and special_type != "Normal"

    # 打分
    score = 0.0
    score += min(25, div_rows * 5)
    if div_growth is not None and div_growth >= 0.05:
        score += 10
    elif div_years:
        score += 5
    score += min(20, buyback_total / 1e8 * 2) if buyback_total else 0
    if holder_conc["top5"] is not None and holder_conc["top5"] >= 40:
        score += 15
    elif holder_conc["top5"] is not None and holder_conc["top5"] >= 25:
        score += 10
    else:
        score += 5
    if holder_trend and holder_trend["change_pct"] < 0:
        score += 5
    if buy_count > sell_count:
        score += 10
    elif buy_count == 0 and sell_count == 0:
        score += 4
    if not pledge_risk:
        score += 15
    if audit_ok is True:
        score += 15
    elif audit_ok is False:
        score -= 15
    if not special_risk:
        score += 5
    score = round(_clamp(score, 0, 100), 1)

    signals: list[str] = []
    if div_years:
        signals.append(f"近 {len(div_years)} 年持续现金分红，最新年度每股 {_f2(dps_latest)} 元，分红增长 {_pct(div_growth) if div_growth else '—'}")
    else:
        signals.append("近年未发现现金分红记录，股东回报依赖股价而非真金白银")
    if buyback_total:
        signals.append(f"近 8 年回购规模合计约 {_yi(buyback_total)}，重视股东回报")
    else:
        signals.append("近年未发现回购记录")
    if holder_conc["top5"] is not None:
        signals.append(f"前五大股东持股 {_f1(holder_conc['top5'])}%，股权集中度{'偏高、结构稳定' if holder_conc['top5'] >= 40 else '一般'}")
    if holder_trend:
        signals.append(holder_trend["text"])
    if buy_count or sell_count:
        signals.append(f"近 8 年股东增减持公告：增持 {buy_count} 条 / 减持 {sell_count} 条")
    if pledge_risk:
        signals.append("存在股权质押记录，需关注大股东流动性风险")
    if audit_ok is False:
        signals.append("审计意见非标准无保留，财务可信度存疑")
    if special_risk:
        signals.append(f"当前特别处理状态：{special_type}")

    return {
        "score": score,
        "dividend_years": div_years,
        "dps_latest": dps_latest,
        "dividend_growth": div_growth,
        "dividend_count": div_rows,
        "buyback_total": buyback_total,
        "buyback_rows": buyback_rows,
        "holder_conc": holder_conc,
        "holder_trend": holder_trend,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "share_changes": latest_changes,
        "pledge_risk": pledge_risk,
        "audit_ok": audit_ok,
        "audit_latest": str(audit_latest.get("opinion") or "") if audit_latest is not None else None,
        "special_type": special_type,
        "signals": signals,
        "payout_ratio": payout,
    }


# --------------------------- 市场时机 --------------------------- #
def market_timing() -> dict:
    end = pdc.latest_trade_date()
    start = (date.today() - timedelta(days=365 * 5)).strftime("%Y%m%d")
    ind = pdc.get_index_indicator("000300.SH", start, end)
    idx = pdc.get_index_daily("000300.SH", start, end)

    pe_pct = pb_pct = None
    pe_latest = pb_latest = None
    if not ind.empty and "pe_ttm" in ind.columns and "pb_ttm" in ind.columns:
        ind = ind.copy()
        pe_s = pd.to_numeric(ind["pe_ttm"], errors="coerce").dropna()
        pb_s = pd.to_numeric(ind["pb_ttm"], errors="coerce").dropna()
        if not pe_s.empty:
            pe_latest = float(pe_s.iloc[-1])
            pe_pct = (pe_s <= pe_latest).mean()
        if not pb_s.empty:
            pb_latest = float(pb_s.iloc[-1])
            pb_pct = (pb_s <= pb_latest).mean()

    ret_20 = ret_60 = ma60_bias = None
    if not idx.empty and "close" in idx.columns:
        close = pd.to_numeric(idx["close"], errors="coerce").dropna()
        if len(close) >= 21:
            ret_20 = float(close.iloc[-1] / close.iloc[-21] - 1)
        if len(close) >= 61:
            ret_60 = float(close.iloc[-1] / close.iloc[-61] - 1)
        if len(close) >= 60:
            ma60 = float(close.tail(60).mean())
            ma60_bias = float(close.iloc[-1] / ma60 - 1)

    score = 50.0
    if pe_pct is not None:
        score = (1 - pe_pct) * 60 + score * 0.2
    if pb_pct is not None:
        score = (1 - pb_pct) * 40 + score * 0.6
    if ret_60 is not None:
        score += _clamp(-ret_60 * 200, -10, 10)
    score = round(_clamp(score, 0, 100), 1)

    if score >= 70:
        sentiment = "市场偏冷、估值处于低位区间（别人恐惧我贪婪）"
    elif score <= 35:
        sentiment = "市场偏热、估值处于高位区间（别人贪婪我恐惧）"
    else:
        sentiment = "市场估值中性，等待个股安全边际"

    signals = [
        f"沪深300 PE(TTM) {_f2(pe_latest)}，处于近 5 年 {_pct(pe_pct) if pe_pct is not None else '—'} 分位",
        f"沪深300 PB(TTM) {_f2(pb_latest)}，处于近 5 年 {_pct(pb_pct) if pb_pct is not None else '—'} 分位",
        f"近 20 日涨跌 {_pct(ret_20) if ret_20 is not None else '—'} / 近 60 日涨跌 {_pct(ret_60) if ret_60 is not None else '—'}",
        f"指数相对 MA60 偏离 {_pct(ma60_bias) if ma60_bias is not None else '—'}",
        sentiment,
    ]
    return {
        "score": score,
        "index": "000300.SH",
        "pe_latest": pe_latest,
        "pe_percentile": pe_pct,
        "pb_latest": pb_latest,
        "pb_percentile": pb_pct,
        "ret_20d": ret_20,
        "ret_60d": ret_60,
        "ma60_bias": ma60_bias,
        "sentiment": sentiment,
        "signals": signals,
    }


# --------------------------- 综合评分 / 仓位 --------------------------- #
def position_advice(score: float, mos: float | None, market_score: float | None) -> dict:
    if score < 50:
        return {
            "action": "不买入",
            "weight": 0,
            "reason": "综合评分低于 50，不满足巴菲特「好公司 + 好价格」双重标准",
            "target_positions": 8,
            "cash_reserve": 0.5,
        }
    mos_v = _clamp(mos, -0.2, 0.6) if mos is not None else 0
    market_v = ((market_score or 50) - 50) / 100
    weight = _clamp(0.10 + (score - 50) / 50 * 0.18 + mos_v * 0.12 + market_v * 0.08, 0.05, 0.40)
    target_positions = 8 if score < 60 else (6 if score < 75 else 5)
    cash_reserve = _clamp(0.45 - (score - 50) / 50 * 0.2 - mos_v * 0.1, 0.15, 0.45)
    return {
        "action": "可分批买入",
        "weight": round(weight, 2),
        "reason": f"综合评分 {score:.0f}，安全边际 {_pct(mos) if mos is not None else '—'}，市场时机 {_f1(market_score) if market_score is not None else '—'}",
        "target_positions": target_positions,
        "cash_reserve": round(cash_reserve, 2),
    }


def _combine(quality: dict, valuation: dict, management: dict, timing: dict) -> dict:
    q = _num(quality.get("score")) or 0
    v = _num(valuation.get("score")) if valuation and valuation.get("score") is not None else 0
    m = _num(management.get("score")) or 0
    t = _num(timing.get("score"))
    if t is None:
        total = q * 0.40 + v * 0.30 + m * 0.30
    else:
        total = q * 0.35 + v * 0.25 + m * 0.20 + t * 0.20
    return {
        "total": round(total, 1),
        "quality": q,
        "valuation": v,
        "management": m,
        "market": t,
    }


# --------------------------- 单股完整分析 --------------------------- #
def full_analysis(query: str) -> dict:
    resolved = pdc.resolve_symbol(query)
    if not resolved["ok"]:
        return {"ok": False, "error": resolved["error"], "data": None}
    symbol = resolved["data"]["symbol"]
    name = resolved["data"]["name"]

    try:
        detail_df = pdc.get_stock_details([symbol], fields=["symbol", "name", "special_type", "listed_date", "sector_code_name", "board_type"])
        detail_row = detail_df.iloc[0] if not detail_df.empty else None
        detail = {
            "symbol": symbol,
            "name": name,
            "special_type": str(detail_row.get("special_type") or "") if detail_row is not None else "",
            "listed_date": str(detail_row.get("listed_date") or "") if detail_row is not None else "",
            "sector": str(detail_row.get("sector_code_name") or "") if detail_row is not None else "",
            "board": str(detail_row.get("board_type") or "") if detail_row is not None else "",
        }
    except Exception:  # noqa: BLE001
        detail = {"symbol": symbol, "name": name, "special_type": "", "listed_date": "", "sector": "", "board": ""}

    try:
        industry = pdc.get_stock_industry(symbol).get("name")
    except Exception:  # noqa: BLE001
        industry = None

    end = pdc.latest_trade_date()
    start_daily = (date.today() - timedelta(days=20)).strftime("%Y%m%d")
    daily = pdc.get_stock_daily(symbol, start_daily, end, fields=["symbol", "date", "close", "name", "trade_status"])
    if daily.empty:
        return {"ok": False, "error": f"未获取到 {symbol} 行情", "data": None}
    price = _num(daily.iloc[-1]["close"])
    if price is None:
        return {"ok": False, "error": f"{symbol} 收盘价缺失", "data": None}

    fina = pdc.get_fina_reports(symbol, "2021q4", "2026q4")
    annuals = _annuals_from_fina(fina)
    quarter_map = _quarter_map(fina)
    if not annuals:
        return {"ok": False, "error": f"{symbol} 缺少年度财务数据", "data": None}
    quality = _quality_from_annuals(annuals, industry)
    shares = _num((annuals[max(annuals)] if annuals else {}).get("shares"))

    try:
        management = management_analysis(symbol, price, shares, detail)
    except Exception as e:  # noqa: BLE001
        management = {"ok": False, "error": str(e), "score": 0, "signals": [f"管理层数据获取失败：{e}"]}
    try:
        timing = market_timing()
    except Exception as e:  # noqa: BLE001
        timing = {"score": None, "signals": [f"市场时机数据获取失败：{e}"], "sentiment": "数据不足"}

    valuation = valuation_analysis(symbol, price, shares, quarter_map, annuals, management.get("dps_latest"), quality)
    combined = _combine(quality, valuation, management, timing)
    advice = position_advice(combined["total"], valuation.get("margin_of_safety"), timing.get("score"))

    summary = {
        "symbol": symbol,
        "name": name,
        "industry": industry,
        "price": price,
        "shares": shares,
        "market_cap": price * shares if shares else None,
        "combined": combined,
        "quality": {
            "score": quality["score"],
            "roe_latest": quality["roe_latest"],
            "roe_mean": quality["roe_mean"],
            "roe_cv": quality["roe_cv"],
            "gross_margin": quality["gross_margin"],
            "net_margin": quality["net_margin"],
            "ocf_ratio_avg": quality["ocf_ratio_avg"],
            "debt_eq": quality["debt_eq"],
            "revenue_cagr": quality["revenue_cagr"],
            "np_cagr": quality["np_cagr"],
            "signals": quality["signals"],
            "annual_table": quality["annual_table"][-6:],
        },
        "valuation": {
            "score": valuation.get("score"),
            "price": price,
            "pe": valuation.get("pe"),
            "pb": valuation.get("pb"),
            "dividend_yield": valuation.get("dividend_yield"),
            "margin_of_safety": valuation.get("margin_of_safety"),
            "owner_earnings": valuation.get("owner_earnings"),
            "bear_value": valuation.get("bear_value"),
            "base_value": valuation.get("base_value"),
            "bull_value": valuation.get("bull_value"),
            "entry_price": valuation.get("entry_price"),
            "verdict": valuation.get("verdict"),
            "signals": valuation.get("signals") or [],
        },
        "management": {
            "score": management.get("score"),
            "dividend_years": management.get("dividend_years"),
            "dps_latest": management.get("dps_latest"),
            "dividend_growth": management.get("dividend_growth"),
            "buyback_total": management.get("buyback_total"),
            "holder_conc": management.get("holder_conc"),
            "holder_trend": management.get("holder_trend"),
            "buy_count": management.get("buy_count"),
            "sell_count": management.get("sell_count"),
            "pledge_risk": management.get("pledge_risk"),
            "audit_ok": management.get("audit_ok"),
            "special_type": management.get("special_type"),
            "signals": management.get("signals") or [],
        },
        "market": {
            "score": timing.get("score"),
            "pe_percentile": timing.get("pe_percentile"),
            "pb_percentile": timing.get("pb_percentile"),
            "sentiment": timing.get("sentiment"),
            "signals": timing.get("signals") or [],
        },
        "advice": advice,
        "detail": detail,
    }

    chart = {
        "kind": "buffett",
        "name": name,
        "symbol": symbol,
        "radar": {
            "indicators": ["盈利能力", "成长性", "现金流", "财务稳健", "管理层", "估值"],
            "values": [
                _quality_radar(quality),
                round(_clamp(((quality.get("np_cagr") or 0) + 0.10) * 500, 0, 100), 1),
                round(_clamp(((quality.get("ocf_ratio_avg") or 0) + 0.2) * 80, 0, 100), 1),
                round(_clamp((1.5 - (quality.get("debt_eq") or 0)) * 50 + 20, 0, 100), 1),
                management.get("score") or 0,
                valuation.get("score") or 0,
            ],
        },
        "valuation": {
            "price": price,
            "bear": valuation.get("bear_value"),
            "base": valuation.get("base_value"),
            "bull": valuation.get("bull_value"),
        },
        "metrics": [
            {"label": "综合评分", "value": f"{combined['total']:.0f}", "tone": _tone(combined["total"])},
            {"label": "企业质量", "value": f"{quality['score']:.0f}", "tone": _tone(quality["score"])},
            {"label": "估值评分", "value": f"{valuation.get('score') or 0:.0f}", "tone": _tone(valuation.get("score") or 0)},
            {"label": "管理层", "value": f"{management.get('score') or 0:.0f}", "tone": _tone(management.get("score") or 0)},
            {"label": "安全边际", "value": _pct(valuation.get("margin_of_safety")) if valuation.get("margin_of_safety") is not None else "—", "tone": "up" if (valuation.get("margin_of_safety") or -1) >= 0 else "down"},
            {"label": "建议仓位", "value": f"{advice['weight'] * 100:.0f}%", "tone": _tone(combined["total"])},
        ],
    }

    return {"ok": True, "error": None, "data": {"summary": summary, "chart": chart}}


def _quality_radar(q: dict) -> float:
    score = q.get("score") or 0
    roe = _num(q.get("roe_mean"))
    if roe is None:
        return 0
    return round(_clamp(roe * 400 + (score / 100) * 30, 0, 100), 1)


def _tone(v: float) -> str:
    return "up" if v >= 70 else ("down" if v < 50 else "mid")


# --------------------------- 全市场筛选 --------------------------- #

# 五位大师的流派元数据（供前端展示与聚合接口）
SCREEN_MASTERS = [
    {"agent": "buffett", "name": "沃伦·巴菲特", "school": "价值投资", "color": "#ef5350",
     "desc": "ROE≥10%、盈利稳定、估值合理，重视护城河与安全边际"},
    {"agent": "lynch", "name": "彼得·林奇", "school": "成长股", "color": "#42a5f5",
     "desc": "低 PEG（PE/成长率）、营收持续增长、负债可控的景气成长股"},
    {"agent": "fisher", "name": "菲利普·费雪", "school": "优质成长", "color": "#26a69a",
     "desc": "高毛利率、研发驱动、营收长期复合增长、强护城河"},
    {"agent": "livermore", "name": "杰西·利弗莫尔", "school": "趋势交易", "color": "#ab47bc",
     "desc": "价格处于上升趋势、量价配合、相对强势的动量股"},
    {"agent": "graham", "name": "本杰明·格雷厄姆", "school": "防御价值", "color": "#ffa726",
     "desc": "低 PB/PE、高股息、价格低于内在价值的破净防御股"},
]


def _screen_prepare(index: str, min_market_cap: float = 5e9, momentum_days: int = 60) -> dict:
    """为筛选准备公共数据：因子、行情、财务、动量、行业映射。"""
    latest = pdc.latest_trade_date()
    factor = pd.DataFrame()
    try:
        cal = pdc.get_trade_cal(
            (date.today() - timedelta(days=15)).strftime("%Y%m%d"),
            latest,
        )
        candidates = sorted(cal["nature_date"].astype(str).tolist(), reverse=True)
    except Exception:  # noqa: BLE001
        candidates = [latest]

    for d in candidates:
        factor = pdc.get_factor(index, d, ["market_cap", "turnover", "amount"])
        if not factor.empty:
            latest = d
            break
    if factor.empty:
        return {"ok": False, "error": f"未获取到 {index} 股票池（已尝试 {candidates[0]} 起最近交易日）", "data": None}
    symbols = factor["symbol"].tolist()

    daily = pdc.get_stock_daily(symbols, latest, latest, fields=["symbol", "date", "close", "name"])
    if daily.empty:
        return {"ok": False, "error": "行情数据为空", "data": None}

    # 动量区间（用于趋势/相对强弱判断）
    start_d = None
    try:
        cal_all = pdc.get_trade_cal(
            (date.today() - timedelta(days=momentum_days + 15)).strftime("%Y%m%d"),
            latest,
        )
        past = [d for d in sorted(cal_all["nature_date"].astype(str).tolist()) if d <= latest]
        start_d = past[-momentum_days] if len(past) >= momentum_days else past[0]
    except Exception:  # noqa: BLE001
        start_d = (date.today() - timedelta(days=momentum_days)).strftime("%Y%m%d")
    hist = pdc.get_stock_daily(symbols, start_d, latest, fields=["symbol", "date", "close", "amount"])

    year = date.today().year
    fina = pdc.get_fina_reports(symbols, f"{year - 3}q4", f"{year - 1}q4")
    if fina.empty:
        return {"ok": False, "error": "财务数据为空", "data": None}
    industry_map = pdc.get_industry_map()

    price_map = dict(zip(daily["symbol"], pd.to_numeric(daily["close"], errors="coerce")))
    name_map = dict(zip(daily["symbol"], daily["name"]))

    # 动量：区间涨跌幅与量能变化
    mom_map: dict[str, dict] = {}
    if not hist.empty:
        hist = hist.copy()
        hist["close"] = pd.to_numeric(hist["close"], errors="coerce")
        for sym, g in hist.groupby("symbol", sort=False):
            g = g.sort_values("date")
            if len(g) >= 2:
                first, last = g.iloc[0]["close"], g.iloc[-1]["close"]
                chg = _safe_div(last - first, first)
                vol_now = pd.to_numeric(g.iloc[-5:]["amount"], errors="coerce").mean() if len(g) >= 5 else None
                vol_prev = pd.to_numeric(g.iloc[:-5]["amount"], errors="coerce").mean() if len(g) > 5 else None
                vol_chg = _safe_div(vol_now, vol_prev) if (vol_now and vol_prev) else None
                mom_map[sym] = {"chg": chg, "vol_chg": vol_chg, "last_close": last}

    return {
        "ok": True, "data": {
            "latest": latest, "symbols": symbols, "factor": factor, "daily": daily,
            "fina": fina, "industry_map": industry_map, "price_map": price_map,
            "name_map": name_map, "mom_map": mom_map, "min_market_cap": min_market_cap,
        }
    }


def _screen_base_rows(prepare: dict) -> list[dict]:
    """把公共数据包展开成每只股票的基础指标行（含财务/估值/动量）。"""
    d = prepare["data"]
    factor, fina = d["factor"], d["fina"]
    industry_map, price_map, name_map, mom_map = d["industry_map"], d["price_map"], d["name_map"], d["mom_map"]
    min_market_cap = d["min_market_cap"]
    rows: list[dict] = []
    by_symbol = fina.groupby("symbol", sort=False)
    for symbol, group in by_symbol:
        annuals = _annuals_from_fina(group)
        if len(annuals) < 2:
            continue
        industry = industry_map.get(symbol)
        q = _quality_from_annuals(annuals, industry)
        price = _num(price_map.get(symbol))
        latest_year = max(annuals)
        latest_metrics = annuals[latest_year]
        shares = _num(latest_metrics.get("shares"))
        mcap = _num((factor.loc[factor["symbol"] == symbol, "market_cap"].iloc[0])) if not factor.loc[factor["symbol"] == symbol, "market_cap"].empty else None
        if price is None or shares is None:
            continue
        mcap = mcap or (price * shares)
        if mcap is None or mcap < min_market_cap:
            continue
        np_latest = _num(latest_metrics.get("net_profit"))
        if not np_latest or np_latest <= 0:
            continue
        prev_metrics = annuals.get(str(int(latest_year) - 1)) or {}
        np_prev = _num(prev_metrics.get("net_profit"))
        np_yoy = _safe_div(np_latest, np_prev) - 1 if np_prev else None
        pe = _safe_div(mcap, np_latest)
        equity = _num(latest_metrics.get("equity"))
        pb = _safe_div(mcap, equity)
        yield_ = _num(latest_metrics.get("dividend_yield"))
        rev_cagr = q.get("revenue_cagr")
        mom = mom_map.get(symbol, {})
        rows.append({
            "symbol": symbol, "name": str(name_map.get(symbol) or symbol),
            "industry": industry or "—", "price": price, "market_cap": mcap,
            "roe": q["roe_latest"], "roe_mean": q["roe_mean"], "np_yoy": np_yoy,
            "pe": pe, "pb": pb, "dividend_yield": yield_,
            "debt_eq": q["debt_eq"], "ocf_ratio": q["ocf_ratio_avg"],
            "gross_margin": q["gross_margin"], "net_margin": q["net_margin"],
            "revenue_cagr": rev_cagr, "rd_ratio": q.get("rd_ratio"),
            "quality_score": q["score"], "momentum_chg": mom.get("chg"),
            "momentum_vol_chg": mom.get("vol_chg"),
        })
    return rows


def _score_buffett(r: dict, industry: str) -> tuple[float, str]:
    if r["roe"] is None or r["roe"] < 0.10:
        return None, "ROE<10%"
    pe, pb = r["pe"], r["pb"]
    val = _valuation_score(pe, pb, r["dividend_yield"], None, _is_financial(industry))
    cv = r.get("roe_cv")
    cv_score = 50 if cv is None else round(_clamp(100 - cv * 250, 0, 100), 1)
    score = round(r["quality_score"] * 0.60 + val * 0.25 + cv_score * 0.15, 1)
    return score, "优秀+合理" if score >= 75 else ("关注" if score >= 65 else "一般")


def _score_lynch(r: dict, industry: str) -> tuple[float, str]:
    # PEG = PE / (净利润增速*100)，越低越好；要求正成长
    g = r["np_yoy"]
    if g is None or g <= 0.05:
        return None, "成长不足"
    pe = r["pe"]
    if pe is None or pe <= 0:
        return None, "PE 无效"
    peg = pe / (g * 100)
    # 评分：PEG<0.5 满分，>2 不及格；叠加营收成长与负债约束
    peg_score = _clamp(100 - (peg - 0.5) * 70, 0, 100)
    rev_score = _clamp(50 + (r["revenue_cagr"] or 0) * 200, 0, 100) if r["revenue_cagr"] is not None else 50
    debt_score = 100 if (r["debt_eq"] is None or r["debt_eq"] < 1.0) else (60 if r["debt_eq"] < 2.0 else 30)
    score = round(peg_score * 0.55 + rev_score * 0.30 + debt_score * 0.15, 1)
    return score, "低 PEG 成长" if score >= 70 else ("关注" if score >= 55 else "一般")


def _score_fisher(r: dict, industry: str) -> tuple[float, str]:
    # 优质成长：高毛利率 + 营收复合增长 + 研发驱动（rd_ratio 若存在）
    gm = r["gross_margin"]
    if gm is None or gm < 0.30:
        return None, "毛利率偏低"
    rev = r["revenue_cagr"]
    if rev is None or rev <= 0.08:
        return None, "成长不足"
    gm_score = _clamp((gm - 0.30) / 0.30 * 100, 0, 100)
    rev_score = _clamp(50 + rev * 220, 0, 100)
    # 稳定性：ROE 持续为正即可给分
    roe_score = _clamp((r["roe"] or 0) * 250, 0, 100) if r["roe"] is not None else 30
    rd = r["rd_ratio"]
    rd_score = _clamp(rd * 300, 0, 100) if rd is not None else 60  # 缺失给中性
    score = round(gm_score * 0.30 + rev_score * 0.35 + roe_score * 0.20 + rd_score * 0.15, 1)
    return score, "优质成长" if score >= 70 else ("关注" if score >= 55 else "一般")


def _score_livermore(r: dict, industry: str) -> tuple[float, str]:
    # 趋势交易：区间涨幅为正且强、量能放大、价格高于近期
    chg = r["momentum_chg"]
    if chg is None or chg <= 0:
        return None, "无上升趋势"
    chg_score = _clamp(50 + chg * 150, 0, 100)
    vol_chg = r["momentum_vol_chg"]
    vol_score = _clamp(50 + (vol_chg or 0) * 40, 0, 100)
    # 配合基本面不暴雷（净利润为正、负债不过高）
    fund_score = 70 if (r["debt_eq"] is None or r["debt_eq"] < 2.5) else 40
    score = round(chg_score * 0.55 + vol_score * 0.25 + fund_score * 0.20, 1)
    return score, "强势动量" if score >= 65 else ("关注" if score >= 50 else "一般")


def _score_graham(r: dict, industry: str) -> tuple[float, str]:
    # 防御价值：低 PB、低 PE、有股息；格雷厄姆式安全边际
    pb = r["pb"]
    if pb is None or pb > 1.5:
        return None, "PB 偏高"
    pe = r["pe"]
    if pe is None or pe > 15:
        return None, "PE 偏高"
    pb_score = _clamp(100 - (pb - 0.5) * 60, 0, 100) if pb >= 0.5 else 100
    pe_score = _clamp(100 - (pe - 8) * 6, 0, 100) if pe >= 8 else 100
    yld = r["dividend_yield"]
    yld_score = _clamp((yld or 0) * 1500, 0, 100)  # 股息率 6.7% 即满分
    score = round(pb_score * 0.40 + pe_score * 0.35 + yld_score * 0.25, 1)
    return score, "破净防御" if score >= 70 else ("关注" if score >= 55 else "一般")


_SCREEN_FUNCS = {
    "buffett": _score_buffett,
    "lynch": _score_lynch,
    "fisher": _score_fisher,
    "livermore": _score_livermore,
    "graham": _score_graham,
}

_SCREEN_METHOD = {
    "buffett": "ROE≥10%、盈利为正、质量60%+估值25%+稳定性15%",
    "lynch": "PEG=PE/(净利增速×100)<1、营收成长、低负债",
    "fisher": "毛利率≥30%、营收复合增长≥8%、研发驱动",
    "livermore": "区间涨幅为正、量能放大、基本面不暴雷",
    "graham": "PB≤1.5、PE≤15、股息率越高越好",
}


def _run_screen(agent: str, index: str, top_n: int, min_market_cap: float) -> dict:
    prep = _screen_prepare(index, min_market_cap)
    if not prep["ok"]:
        return {"ok": False, "error": prep["error"], "data": None}
    base_rows = _screen_base_rows(prep)
    if not base_rows:
        return {"ok": False, "error": f"{index} 中没有基础财务数据达标的股票", "data": None}
    scorer = _SCREEN_FUNCS[agent]
    meta = next(m for m in SCREEN_MASTERS if m["agent"] == agent)
    rows: list[dict] = []
    for r in base_rows:
        sc, verdict = scorer(r, r["industry"])
        if sc is None:
            continue
        rows.append({
            "symbol": r["symbol"], "name": r["name"], "industry": r["industry"],
            "price": r["price"], "market_cap": r["market_cap"], "roe": r["roe"],
            "np_yoy": r["np_yoy"], "pe": r["pe"], "pb": r["pb"],
            "dividend_yield": r["dividend_yield"], "revenue_cagr": r["revenue_cagr"],
            "momentum_chg": r["momentum_chg"], "total_score": sc, "verdict": verdict,
        })
    if not rows:
        return {"ok": False, "error": f"{index} 中没有满足「{meta['name']}」筛选标准的股票", "data": None}
    total_matched = len(rows)
    rows.sort(key=lambda r: r["total_score"], reverse=True)
    rows = rows[:max(1, min(int(top_n), 50))]
    latest = prep["data"]["latest"]
    summary = {
        "agent": agent, "master": meta["name"], "school": meta["school"],
        "index": index, "date": latest, "scanned": len(prep["data"]["symbols"]),
        "matched": total_matched, "returned": len(rows), "top": rows,
        "method": _SCREEN_METHOD[agent],
    }
    chart = {
        "kind": "screen", "agent": agent, "master": meta["name"], "school": meta["school"],
        "color": meta["color"], "index": index,
        "labels": [r["name"] for r in rows],
        "values": [r["total_score"] for r in rows],
        "detail": [
            {"name": r["name"], "symbol": r["symbol"], "industry": r["industry"],
             "price": _f2(r["price"]), "roe": _pct(r["roe"]), "pe": _f2(r["pe"]),
             "pb": _f2(r["pb"]), "score": r["total_score"], "verdict": r["verdict"]}
            for r in rows
        ],
    }
    return {"ok": True, "error": None, "data": {"summary": summary, "chart": chart}}


def screen(index: str = "000300", top_n: int = 20, min_market_cap: float = 5e9) -> dict:
    """巴菲特价值投资筛选（兼容原接口）。"""
    return _run_screen("buffett", index, top_n, min_market_cap)


def screen_by_master(agent: str, index: str = "000300", top_n: int = 20, min_market_cap: float = 5e9) -> dict:
    """按指定大师流派筛选：buffett / lynch / fisher / livermore / graham。"""
    if agent not in _SCREEN_FUNCS:
        return {"ok": False, "error": f"未知大师流派：{agent}", "data": None}
    return _run_screen(agent, index, top_n, min_market_cap)


def screen_all(index: str = "000300", top_n: int = 10, min_market_cap: float = 5e9) -> dict:
    """五位大师各自筛选，返回聚合结果（含每位大师的 top 列表与交集）。"""
    out = {"ok": True, "error": None, "data": {"index": index, "masters": [], "intersection": []}}
    by_symbol: dict[str, dict] = {}
    for m in SCREEN_MASTERS:
        res = _run_screen(m["agent"], index, top_n, min_market_cap)
        entry = {
            "agent": m["agent"], "name": m["name"], "school": m["school"],
            "color": m["color"], "desc": m["desc"], "method": _SCREEN_METHOD[m["agent"]],
            "ok": res["ok"], "error": res.get("error"),
        }
        if res["ok"]:
            entry["summary"] = res["data"]["summary"]
            entry["chart"] = res["data"]["chart"]
            for r in res["data"]["summary"]["top"]:
                rec = by_symbol.setdefault(r["symbol"], {"symbol": r["symbol"], "name": r["name"], "count": 0, "masters": []})
                rec["count"] += 1
                rec["masters"].append(m["name"])
        out["data"]["masters"].append(entry)
    # 被多位大师共同选中的标的（交集）
    inter = sorted(by_symbol.values(), key=lambda x: x["count"], reverse=True)
    out["data"]["intersection"] = inter[: max(1, min(top_n, 20))]
    return out
    latest = pdc.latest_trade_date()
    factor = pd.DataFrame()
    try:
        cal = pdc.get_trade_cal(
            (date.today() - timedelta(days=15)).strftime("%Y%m%d"),
            latest,
        )
        candidates = sorted(cal["nature_date"].astype(str).tolist(), reverse=True)
    except Exception:  # noqa: BLE001
        candidates = [latest]

    # 当日数据未就绪时自动回退到最近有数据的交易日
    for d in candidates:
        factor = pdc.get_factor(index, d, ["market_cap", "turnover", "amount"])
        if not factor.empty:
            latest = d
            break
    if factor.empty:
        return {"ok": False, "error": f"未获取到 {index} 股票池（已尝试 {candidates[0]} 起最近交易日）", "data": None}
    symbols = factor["symbol"].tolist()

    daily = pdc.get_stock_daily(symbols, latest, latest, fields=["symbol", "date", "close", "name"])
    if daily.empty:
        return {"ok": False, "error": "行情数据为空", "data": None}

    year = date.today().year
    fina = pdc.get_fina_reports(symbols, f"{year - 3}q4", f"{year - 1}q4")
    if fina.empty:
        return {"ok": False, "error": "财务数据为空", "data": None}
    industry_map = pdc.get_industry_map()

    price_map = dict(zip(daily["symbol"], pd.to_numeric(daily["close"], errors="coerce")))
    name_map = dict(zip(daily["symbol"], daily["name"]))
    rows: list[dict] = []

    by_symbol = fina.groupby("symbol", sort=False)
    for symbol, group in by_symbol:
        annuals = _annuals_from_fina(group)
        if len(annuals) < 2:
            continue
        industry = industry_map.get(symbol)
        q = _quality_from_annuals(annuals, industry)
        price = _num(price_map.get(symbol))
        latest_year = max(annuals)
        latest_metrics = annuals[latest_year]
        shares = _num(latest_metrics.get("shares"))
        mcap = _num((factor.loc[factor["symbol"] == symbol, "market_cap"].iloc[0])) if not factor.loc[factor["symbol"] == symbol, "market_cap"].empty else None
        if price is None or shares is None:
            continue
        mcap = mcap or (price * shares)
        if mcap is None or mcap < min_market_cap:
            continue

        np_latest = _num(latest_metrics.get("net_profit"))
        if not np_latest or np_latest <= 0:
            continue
        roe = _num(latest_metrics.get("roe"))
        if roe is None or roe < 0.10:
            continue

        prev_metrics = annuals.get(str(int(latest_year) - 1)) or {}
        np_prev = _num(prev_metrics.get("net_profit"))
        np_yoy = _safe_div(np_latest, np_prev) - 1 if np_prev else None
        pe = _safe_div(mcap, np_latest)
        equity = _num(latest_metrics.get("equity"))
        pb = _safe_div(mcap, equity)
        score = round(q["score"] * 0.60 + _valuation_score(pe, pb, None, None, _is_financial(industry)) * 0.25 + _cv_score(q) * 0.15, 1)

        rows.append({
            "symbol": symbol,
            "name": str(name_map.get(symbol) or symbol),
            "industry": industry or "—",
            "price": price,
            "market_cap": mcap,
            "roe": roe,
            "roe_mean": q["roe_mean"],
            "np_yoy": np_yoy,
            "pe": pe,
            "pb": pb,
            "debt_eq": q["debt_eq"],
            "ocf_ratio": q["ocf_ratio_avg"],
            "gross_margin": q["gross_margin"],
            "quality_score": q["score"],
            "valuation_score": _valuation_score(pe, pb, None, None, _is_financial(industry)),
            "total_score": score,
            "verdict": "优秀+合理" if score >= 75 else ("关注" if score >= 65 else "一般"),
        })

    if not rows:
        return {"ok": False, "error": f"{index} 中没有满足最低巴菲特标准的股票", "data": None}
    total_matched = len(rows)
    rows.sort(key=lambda r: r["total_score"], reverse=True)
    rows = rows[:max(1, min(int(top_n), 50))]

    summary = {
        "index": index,
        "date": latest,
        "scanned": len(symbols),
        "matched": total_matched,
        "returned": len(rows),
        "top": rows,
        "method": "ROE≥10%、净利润为正、市值≥50亿，质量60% + 估值25% + 稳定性15%",
    }
    chart = {
        "kind": "screen",
        "index": index,
        "labels": [r["name"] for r in rows],
        "values": [r["total_score"] for r in rows],
        "detail": [
            {"name": r["name"], "symbol": r["symbol"], "industry": r["industry"],
             "price": _f2(r["price"]), "roe": _pct(r["roe"]), "pe": _f2(r["pe"]),
             "pb": _f2(r["pb"]), "score": r["total_score"]}
            for r in rows
        ],
    }
    return {"ok": True, "error": None, "data": {"summary": summary, "chart": chart}}


def _cv_score(q: dict) -> float:
    cv = q.get("roe_cv")
    if cv is None:
        return 50
    return round(_clamp(100 - cv * 250, 0, 100), 1)


# --------------------------- CLI --------------------------- #
def _summary_to_markdown(s: dict) -> str:
    lines: list[str] = []
    lines.append(f"# {s['name']}（{s['symbol']}）巴菲特价值投资报告\n")
    lines.append(f"- 行业：{s['industry'] or '—'}｜现价：{_f2(s['price'])}｜总市值：{_yi(s['market_cap'])}")
    lines.append(f"- 综合评分：**{s['combined']['total']:.0f}**（质量 {s['combined']['quality']:.0f} / 估值 {s['combined']['valuation']:.0f} / 管理层 {s['combined']['management']:.0f} / 市场 {s['combined']['market'] if s['combined']['market'] is not None else '—'}）\n")
    lines.append("## 一、企业质量与护城河")
    lines.extend(f"- {x}" for x in s["quality"]["signals"])
    lines.append("\n年度财务（最近 5 年）：")
    lines.append("| 年份 | 营收 | 净利润 | ROE | 毛利率 | 净利率 | 现金流/净利 | 负债/净资产 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in s["quality"]["annual_table"][-5:]:
        lines.append(
            f"| {r['year']} | {_yi(r['revenue'])} | {_yi(r['net_profit'])} | {_pct(r['roe'])} "
            f"| {_pct(r['gross_margin'])} | {_pct(r['net_margin'])} | {_f2(r['ocf_ratio'])} | {_f2(r['debt_eq'])} |"
        )
    lines.append("\n## 二、估值与安全边际")
    lines.extend(f"- {x}" for x in s["valuation"]["signals"])
    lines.append(f"- 结论：**{s['valuation']['verdict']}**")
    lines.append("\n## 三、管理层与股东回报")
    lines.extend(f"- {x}" for x in s["management"]["signals"])
    lines.append("\n## 四、市场环境与时机")
    lines.extend(f"- {x}" for x in s["market"]["signals"])
    lines.append("\n## 五、交易计划")
    a = s["advice"]
    lines.append(f"- 动作：**{a['action']}**｜建议仓位：**{a['weight'] * 100:.0f}%**｜目标持仓数：{a['target_positions']} 只｜现金保留：{a['cash_reserve'] * 100:.0f}%")
    lines.append(f"- 理由：{a['reason']}")
    lines.append(f"- 建议买点：{_f2(s['valuation']['entry_price']) if s['valuation']['entry_price'] else '—'}；内在价值区间：{_f2(s['valuation']['bear_value'])} ~ {_f2(s['valuation']['bull_value'])}")
    lines.append("\n## 六、风险提示")
    lines.append("- 以上分析基于公开财务与行情数据，存在披露滞后与统计口径差异；仅供研究学习，不构成任何投资建议。")
    return "\n".join(lines)


def _report_markdown(query: str) -> str:
    res = full_analysis(query)
    if not res["ok"]:
        return f"分析失败：{res['error']}"
    return _summary_to_markdown(res["data"]["summary"])


def _screen_markdown(index: str, top_n: int, master: str = "buffett") -> str:
    res = screen_by_master(master, index, top_n) if master != "buffett" else screen(index, top_n)
    if not res["ok"]:
        return f"筛选失败：{res['error']}"
    s = res["data"]["summary"]
    master_name = s.get("master", "巴菲特")
    school = s.get("school", "价值投资")
    lines = [f"# {s['index']} {master_name}（{school}）风格筛选（{s['date']}）\n"]
    lines.append(f"扫描 {s['scanned']} 只，符合条件 {s['matched']} 只。规则：{s['method']}\n")
    lines.append("| 排名 | 代码 | 名称 | 行业 | 现价 | 市值 | ROE | 净利增速 | PE | PB | 总评分 | 结论 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, r in enumerate(s["top"], 1):
        lines.append(
            f"| {i} | {r['symbol']} | {r['name']} | {r['industry']} | {_f2(r['price'])} "
            f"| {_yi(r['market_cap'])} | {_pct(r['roe'])} | {_pct(r['np_yoy'])} | {_f2(r['pe'])} "
            f"| {_f2(r['pb'])} | **{r['total_score']:.0f}** | {r['verdict']} |"
        )
    lines.append("\n仅供研究学习，不构成任何投资建议。")
    return "\n".join(lines)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    if args and args[0] in ("--screen", "-s"):
        idx = args[1] if len(args) > 1 and not args[1].startswith("-") else "000300"
        n = 20
        if "--top" in args:
            n = int(args[args.index("--top") + 1])
        print(_screen_markdown(idx, n))
    elif args:
        print(_report_markdown(args[0]))
    else:
        print("用法：python -m app.tools.buffett 600519 | python -m app.tools.buffett --screen 000300 --top 20")
