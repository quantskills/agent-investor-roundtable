"""宏观分析工具。

汇总主要宏观经济指标（GDP/CPI/PPI/PMI/M2）与大盘指数近期走势，
输出：
- summary：给 LLM 阅读的宏观快照 + 定性判断
- chart：给前端渲染的大盘指数走势与宏观指标卡片
"""
from __future__ import annotations

import json

import pandas as pd

from app.data.akshare_client import get_index_daily, get_macro_indicators


def _index_trend(index_code: str) -> dict | None:
    """计算某大盘指数近期涨跌与走势数据。"""
    r = get_index_daily(index_code, days=60)
    if not r["ok"]:
        return None
    df: pd.DataFrame = r["data"]["df"]
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if close.empty:
        return None
    latest = float(close.iloc[-1])
    ret_5 = round((latest / float(close.iloc[-6]) - 1) * 100, 2) if len(close) >= 6 else None
    ret_20 = round((latest / float(close.iloc[-21]) - 1) * 100, 2) if len(close) >= 21 else None
    ma20 = round(float(close.tail(20).mean()), 2)
    stance = "强于均线（偏多）" if latest > ma20 else "弱于均线（偏空）"
    return {
        "name": r["data"]["name"],
        "code": index_code,
        "close": round(latest, 2),
        "ret_5d_pct": ret_5,
        "ret_20d_pct": ret_20,
        "ma20": ma20,
        "stance": stance,
        "dates": df["date"].tolist(),
        "closes": [round(float(c), 2) if pd.notna(c) else None for c in df["close"]],
    }


def snapshot() -> dict:
    """生成宏观快照。"""
    macro = get_macro_indicators()
    indicators = macro["data"] if macro["ok"] else {}

    # 定性解读
    readings: list[str] = []
    for name, item in (indicators or {}).items():
        if not item or item.get("error"):
            continue
        latest = item.get("latest")
        trend = item.get("trend")
        if latest is None:
            continue
        readings.append(f"{name} 最新 {latest}（{trend or '—'}）")

    # 大盘指数
    indices = []
    for code in ["sh000001", "sz399001", "sz399006"]:
        t = _index_trend(code)
        if t:
            indices.append(t)

    market_bias = None
    if indices:
        up = sum(1 for i in indices if (i.get("ret_20d_pct") or 0) > 0)
        if up >= 2:
            market_bias = "近 20 日多数主要指数上行，市场系统性风险较低、情绪偏暖"
        elif up == 0:
            market_bias = "近 20 日主要指数普遍下行，系统性风险偏高、需谨慎"
        else:
            market_bias = "主要指数走势分化，市场处于震荡格局"

    summary = {
        "indicators": indicators,
        "readings": readings,
        "market_bias": market_bias,
        "indices_brief": [
            {"name": i["name"], "close": i["close"],
             "ret_20d_pct": i["ret_20d_pct"], "stance": i["stance"]}
            for i in indices
        ],
    }

    chart = {
        "indicator_cards": [
            {"name": n, "value": v.get("latest"), "trend": v.get("trend")}
            for n, v in (indicators or {}).items()
            if v and not v.get("error") and v.get("latest") is not None
        ],
        "indices": [
            {"name": i["name"], "dates": i["dates"], "closes": i["closes"]}
            for i in indices
        ],
    }

    return {"ok": True, "error": None, "data": {"summary": summary, "chart": chart}}


if __name__ == "__main__":
    res = snapshot()
    print(json.dumps(res["data"]["summary"], ensure_ascii=False, indent=2))
