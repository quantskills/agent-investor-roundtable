"""akshare 数据封装层。

- 统一符号解析：名称 / 6 位代码 / 带市场前缀 -> 标准 6 位代码
- 行情：个股日线、大盘指数日线
- 宏观：GDP / CPI / PPI / PMI / M2 / 利率
- 带简单 TTL 内存缓存，所有函数捕获异常并返回 {"ok", "data"/"error"} 结构，
  保证上层 Agent 不会因数据源抖动而崩溃。
"""
from __future__ import annotations

import time
from typing import Any, Callable

import akshare as ak
import pandas as pd

# --------------------------- 简易 TTL 缓存 --------------------------- #
_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: int, producer: Callable[[], Any]) -> Any:
    """带 TTL 的内存缓存。producer 抛异常时不写缓存。"""
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = producer()
    _CACHE[key] = (now, value)
    return value


def _ok(data: Any) -> dict:
    return {"ok": True, "data": data, "error": None}


def _err(msg: str) -> dict:
    return {"ok": False, "data": None, "error": msg}


# --------------------------- 符号解析 --------------------------- #
def _load_code_name() -> pd.DataFrame:
    return _cached(
        "code_name_map",
        ttl=60 * 60 * 12,
        producer=lambda: ak.stock_info_a_code_name(),
    )


def resolve_symbol(query: str) -> dict:
    """把「贵州茅台 / 600519 / sh600519 / SH600519」解析为标准 6 位代码。

    返回 {"ok": True, "data": {"symbol": "600519", "name": "贵州茅台"}}。
    """
    if not query:
        return _err("查询内容为空")
    raw = query.strip()
    # 去掉市场前缀
    cleaned = raw.lower().replace("sh", "").replace("sz", "").replace("bj", "").strip()
    try:
        df = _load_code_name()
    except Exception as e:  # noqa: BLE001
        # 无法加载映射表时，若输入本身就是 6 位数字则直接使用
        if cleaned.isdigit() and len(cleaned) == 6:
            return _ok({"symbol": cleaned, "name": cleaned})
        return _err(f"无法加载代码表：{e}")

    # 情况 1：纯 6 位数字代码
    if cleaned.isdigit() and len(cleaned) == 6:
        row = df[df["code"] == cleaned]
        name = row.iloc[0]["name"] if not row.empty else cleaned
        return _ok({"symbol": cleaned, "name": name})

    # 情况 2：按名称精确 / 模糊匹配
    exact = df[df["name"] == raw]
    if not exact.empty:
        r = exact.iloc[0]
        return _ok({"symbol": r["code"], "name": r["name"]})
    fuzzy = df[df["name"].str.contains(raw, na=False)]
    if not fuzzy.empty:
        r = fuzzy.iloc[0]
        return _ok({"symbol": r["code"], "name": r["name"]})

    return _err(f"未找到匹配的股票：{query}")


# --------------------------- 个股日线 --------------------------- #
def _prefixed(symbol: str) -> str:
    """为 6 位代码推断市场前缀，用于 Sina 接口（sh/sz/bj）。"""
    if symbol.startswith(("sh", "sz", "bj")):
        return symbol
    if symbol.startswith("6"):
        return f"sh{symbol}"
    if symbol.startswith(("0", "3")):
        return f"sz{symbol}"
    if symbol.startswith(("4", "8")):
        return f"bj{symbol}"
    return f"sh{symbol}"


def _fetch_sina(symbol: str, days: int) -> pd.DataFrame:
    """主源：新浪 stock_zh_a_daily（前复权）。"""
    df = ak.stock_zh_a_daily(symbol=_prefixed(symbol), adjust="qfq")
    keep = [c for c in ["date", "open", "close", "high", "low", "volume"] if c in df.columns]
    df = df[keep].copy()
    df["date"] = df["date"].astype(str)
    return df.tail(days).reset_index(drop=True)


def _fetch_em(symbol: str, days: int) -> pd.DataFrame:
    """备源：东财 stock_zh_a_hist（前复权）。"""
    df = ak.stock_zh_a_hist(symbol=symbol, period="daily", adjust="qfq")
    rename = {"日期": "date", "开盘": "open", "收盘": "close",
              "最高": "high", "最低": "low", "成交量": "volume"}
    df = df.rename(columns=rename)
    keep = [c for c in ["date", "open", "close", "high", "low", "volume"] if c in df.columns]
    df = df[keep].copy()
    df["date"] = df["date"].astype(str)
    return df.tail(days).reset_index(drop=True)


def get_daily_kline(symbol: str, days: int = 250) -> dict:
    """获取个股前复权日线，返回标准化 DataFrame（列：date/open/close/high/low/volume）。

    先试新浪源，失败自动回退东财源；两者皆有一次重试，提升数据源抖动时的成功率。
    """

    def _fetch() -> pd.DataFrame:
        last_err: Exception | None = None
        for fetcher in (_fetch_sina, _fetch_em):
            for _attempt in range(2):
                try:
                    df = fetcher(symbol, days)
                    if df is not None and not df.empty:
                        return df
                except Exception as e:  # noqa: BLE001
                    last_err = e
        if last_err:
            raise last_err
        raise RuntimeError("行情数据为空")

    try:
        df = _cached(f"kline:{symbol}:{days}", ttl=60 * 30, producer=_fetch)
        if df is None or df.empty:
            return _err(f"未获取到 {symbol} 的行情数据")
        return _ok(df)
    except Exception as e:  # noqa: BLE001
        return _err(f"获取行情失败：{e}")


# --------------------------- 大盘指数 --------------------------- #
_INDEX_MAP = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
}


def get_index_daily(index_code: str = "sh000001", days: int = 120) -> dict:
    """获取大盘指数日线。index_code 形如 sh000001 / sz399001 / sz399006。"""

    def _fetch() -> pd.DataFrame:
        df = ak.stock_zh_index_daily(symbol=index_code)
        df = df.rename(columns={"date": "date"})
        df["date"] = df["date"].astype(str)
        return df.tail(days).reset_index(drop=True)

    try:
        df = _cached(f"index:{index_code}:{days}", ttl=60 * 30, producer=_fetch)
        if df is None or df.empty:
            return _err(f"未获取到指数 {index_code} 数据")
        return _ok({"name": _INDEX_MAP.get(index_code, index_code), "df": df})
    except Exception as e:  # noqa: BLE001
        return _err(f"获取指数失败：{e}")


# --------------------------- 宏观数据 --------------------------- #
def _latest_two(df: pd.DataFrame, value_col: str) -> dict | None:
    """从宏观时间序列中取最近两期，计算方向。"""
    if df is None or df.empty or value_col not in df.columns:
        return None
    s = pd.to_numeric(df[value_col], errors="coerce").dropna()
    if s.empty:
        return None
    latest = float(s.iloc[-1])
    prev = float(s.iloc[-2]) if len(s) >= 2 else None
    trend = None
    if prev is not None:
        trend = "上行" if latest > prev else ("下行" if latest < prev else "持平")
    return {"latest": latest, "prev": prev, "trend": trend}


def get_macro_indicators() -> dict:
    """汇总主要宏观指标最新值与方向。单个指标失败不影响其它。"""

    def _fetch() -> dict:
        out: dict[str, Any] = {}

        def safe(name: str, fn: Callable[[], pd.DataFrame], col_guess: list[str]) -> None:
            try:
                df = fn()
                col = next((c for c in col_guess if c in df.columns), None)
                if col is None:
                    # 退而取最后一个数值列
                    num_cols = [c for c in df.columns if c != df.columns[0]]
                    col = num_cols[-1] if num_cols else None
                out[name] = _latest_two(df, col) if col else None
            except Exception as e:  # noqa: BLE001
                out[name] = {"error": str(e)}

        safe("GDP同比", ak.macro_china_gdp_yearly, ["今值", "value"])
        safe("CPI同比", ak.macro_china_cpi_monthly, ["今值", "value"])
        safe("PPI同比", ak.macro_china_ppi_yearly, ["今值", "value"])
        safe("PMI", ak.macro_china_pmi_yearly, ["今值", "value"])
        safe("M2同比", ak.macro_china_m2_yearly, ["今值", "value"])
        return out

    try:
        data = _cached("macro:indicators", ttl=60 * 60 * 6, producer=_fetch)
        return _ok(data)
    except Exception as e:  # noqa: BLE001
        return _err(f"获取宏观数据失败：{e}")


# --------------------------- 手动验证入口 --------------------------- #
if __name__ == "__main__":
    print("== resolve_symbol('贵州茅台') ==")
    print(resolve_symbol("贵州茅台"))
    print("\n== get_daily_kline('600519') 尾部 ==")
    r = get_daily_kline("600519", days=5)
    if r["ok"]:
        print(r["data"])
    else:
        print(r["error"])
    print("\n== get_macro_indicators() ==")
    print(get_macro_indicators())
