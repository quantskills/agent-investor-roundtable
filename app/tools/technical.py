"""技术分析工具。

纯 pandas / numpy 实现常见技术指标（MA/EMA/MACD/RSI/KDJ/BOLL 与量能），
并给出信号判断。输出包含：
- summary：给 LLM 阅读的最新指标值 + 信号判断
- chart：给前端 ECharts 绘图的时间序列
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from app.data.pandadata_client import get_daily_kline, resolve_symbol


# --------------------------- 指标计算 --------------------------- #
def _ma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    dif = _ema(close, fast) - _ema(close, slow)
    dea = _ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def _rsi(close: pd.Series, n: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _kdj(df: pd.DataFrame, n: int = 9):
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def _boll(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = _ma(close, n)
    std = close.rolling(n).std()
    return mid + k * std, mid, mid - k * std


def _f(v) -> float | None:
    """安全转 float，NaN -> None。"""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    try:
        f = float(v)
        return None if np.isnan(f) else round(f, 3)
    except (TypeError, ValueError):
        return None


# --------------------------- 信号判断 --------------------------- #
def _build_signals(df: pd.DataFrame) -> list[str]:
    signals: list[str] = []
    last = df.iloc[-1]

    # 均线多空排列
    ma5, ma20, ma60 = last.get("ma5"), last.get("ma20"), last.get("ma60")
    if pd.notna(ma5) and pd.notna(ma20) and pd.notna(ma60):
        if ma5 > ma20 > ma60:
            signals.append("均线多头排列（MA5>MA20>MA60），中短期趋势偏强")
        elif ma5 < ma20 < ma60:
            signals.append("均线空头排列（MA5<MA20<MA60），中短期趋势偏弱")
        else:
            signals.append("均线交织，趋势不明朗")

    # MACD 金叉/死叉
    if len(df) >= 2:
        prev, cur = df.iloc[-2], df.iloc[-1]
        if pd.notna(cur["dif"]) and pd.notna(cur["dea"]):
            if prev["dif"] <= prev["dea"] and cur["dif"] > cur["dea"]:
                signals.append("MACD 金叉，短期动能转强")
            elif prev["dif"] >= prev["dea"] and cur["dif"] < cur["dea"]:
                signals.append("MACD 死叉，短期动能转弱")
            elif cur["hist"] > 0:
                signals.append("MACD 红柱，多头动能延续")
            else:
                signals.append("MACD 绿柱，空头动能延续")

    # RSI 超买超卖
    rsi = last.get("rsi12")
    if pd.notna(rsi):
        if rsi >= 80:
            signals.append(f"RSI(12)={rsi:.1f} 超买，警惕回调")
        elif rsi <= 20:
            signals.append(f"RSI(12)={rsi:.1f} 超卖，可能超跌反弹")
        else:
            signals.append(f"RSI(12)={rsi:.1f} 处于中性区间")

    # KDJ
    k, d = last.get("k"), last.get("d")
    if pd.notna(k) and pd.notna(d):
        if k > d and k < 20:
            signals.append("KDJ 低位金叉迹象，关注反弹")
        elif k < d and k > 80:
            signals.append("KDJ 高位死叉迹象，注意风险")

    # 量价
    if "vol_ma5" in df.columns and pd.notna(last.get("vol_ma5")):
        if last["volume"] > 1.5 * last["vol_ma5"]:
            signals.append("成交量显著放大（>1.5倍5日均量），量能配合")
        elif last["volume"] < 0.6 * last["vol_ma5"]:
            signals.append("成交量明显萎缩，观望情绪浓")

    return signals


# --------------------------- 主入口 --------------------------- #
def analyze(query: str, days: int = 250) -> dict:
    """对给定股票做技术分析。query 可为代码或名称。"""
    resolved = resolve_symbol(query)
    if not resolved["ok"]:
        return {"ok": False, "error": resolved["error"], "data": None}
    symbol = resolved["data"]["symbol"]
    name = resolved["data"]["name"]

    try:
        df: pd.DataFrame = get_daily_kline(symbol, days=days)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"获取行情失败：{e}", "data": None}
    if df is None or df.empty:
        return {"ok": False, "error": f"未获取到 {symbol} 的行情数据", "data": None}
    df = df.copy()
    for col in ["open", "close", "high", "low", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    close = df["close"]
    df["ma5"], df["ma10"], df["ma20"], df["ma60"] = (
        _ma(close, 5), _ma(close, 10), _ma(close, 20), _ma(close, 60)
    )
    df["dif"], df["dea"], df["hist"] = _macd(close)
    df["rsi6"], df["rsi12"], df["rsi24"] = _rsi(close, 6), _rsi(close, 12), _rsi(close, 24)
    df["k"], df["d"], df["j"] = _kdj(df)
    df["boll_up"], df["boll_mid"], df["boll_low"] = _boll(close)
    df["vol_ma5"] = _ma(df["volume"], 5)

    last = df.iloc[-1]
    pct = None
    if len(df) >= 2 and pd.notna(df.iloc[-2]["close"]):
        pct = round((last["close"] / df.iloc[-2]["close"] - 1) * 100, 2)

    summary = {
        "symbol": symbol,
        "name": name,
        "date": str(last["date"]),
        "close": _f(last["close"]),
        "change_pct": pct,
        "ma": {"MA5": _f(last["ma5"]), "MA10": _f(last["ma10"]),
               "MA20": _f(last["ma20"]), "MA60": _f(last["ma60"])},
        "macd": {"DIF": _f(last["dif"]), "DEA": _f(last["dea"]), "HIST": _f(last["hist"])},
        "rsi": {"RSI6": _f(last["rsi6"]), "RSI12": _f(last["rsi12"]), "RSI24": _f(last["rsi24"])},
        "kdj": {"K": _f(last["k"]), "D": _f(last["d"]), "J": _f(last["j"])},
        "boll": {"UP": _f(last["boll_up"]), "MID": _f(last["boll_mid"]), "LOW": _f(last["boll_low"])},
        "signals": _build_signals(df),
    }

    # 前端绘图数据（裁剪最近 120 根，减小体积）
    plot = df.tail(120)
    chart = {
        "symbol": symbol,
        "name": name,
        "dates": plot["date"].tolist(),
        "kline": plot[["open", "close", "low", "high"]].round(3).values.tolist(),
        "volume": [_f(v) for v in plot["volume"]],
        "ma5": [_f(v) for v in plot["ma5"]],
        "ma20": [_f(v) for v in plot["ma20"]],
        "ma60": [_f(v) for v in plot["ma60"]],
        "dif": [_f(v) for v in plot["dif"]],
        "dea": [_f(v) for v in plot["dea"]],
        "macd_hist": [_f(v) for v in plot["hist"]],
        "rsi12": [_f(v) for v in plot["rsi12"]],
    }

    return {"ok": True, "error": None, "data": {"summary": summary, "chart": chart}}


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "600519"
    res = analyze(q)
    if res["ok"]:
        print(json.dumps(res["data"]["summary"], ensure_ascii=False, indent=2))
    else:
        print("ERROR:", res["error"])
