"""panda_data 数据封装层。

- 统一符号解析：名称 / 6 位代码 / 带交易所后缀代码 -> panda_data 格式
- 行情：个股日线（前复权/不复权）、指数日线、指数估值
- 基本面：财务季度报告、业绩快报、审计意见
- 公司治理：分红、回购、股东、增减持、质押、股权集中度
- 股票池：指数成分股（get_factor + index_component）、行业映射
- 所有数据走 panda_data SDK，带 TTL 内存缓存，接口异常向上抛出由工具层兜底
"""
from __future__ import annotations

import os
import re
import time
from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd
import panda_data as pdapi
from dotenv import load_dotenv

load_dotenv()

_CACHE: dict[str, tuple[float, Any]] = {}
_TOKEN_READY = False


# --------------------------- 基础设施 --------------------------- #
def _cached(key: str, ttl: int, producer: Callable[[], Any]) -> Any:
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = producer()
    _CACHE[key] = (now, value)
    return value


def _call(key: str, ttl: int, fn: Callable[[], Any]) -> Any:
    def producer() -> Any:
        ensure_token()
        return fn()

    return _cached(key, ttl, producer)


def ensure_token() -> None:
    """使用 .env 中的账号初始化 panda_data token，只初始化一次。"""
    global _TOKEN_READY
    if _TOKEN_READY:
        return
    username = os.getenv("PANDA_USERNAME", "").strip()
    password = os.getenv("PANDA_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError("未配置 PANDA_USERNAME / PANDA_PASSWORD（请复制 .env.example 为 .env 并填写）")
    pdapi.init_token(username=username, password=password)
    _TOKEN_READY = True


def _normalize_date(value: str) -> str:
    return value.replace("-", "").replace("/", "")


def _today() -> str:
    return date.today().strftime("%Y%m%d")


def _to_frame(result: Any) -> pd.DataFrame:
    if result is None:
        return pd.DataFrame()
    if isinstance(result, pd.DataFrame):
        return result
    if isinstance(result, (str, int, float)):
        return pd.DataFrame({"value": [result]})
    return pd.DataFrame()


# --------------------------- 全市场基础信息 --------------------------- #
def all_stock_details() -> pd.DataFrame:
    """全部 A 股基础信息（含名称、上市状态、特别处理标记）。"""
    def producer() -> pd.DataFrame:
        df = pdapi.get_stock_detail(symbol=[], fields=["symbol", "name", "special_type", "status"], status=1)
        return _to_frame(df)

    return _call("pd:all_details", ttl=60 * 60 * 12, fn=producer)


def resolve_symbol(query: str) -> dict:
    """把「贵州茅台 / 600519 / 600519.SH」解析为 panda_data 符号。

    返回 {"ok": True, "data": {"symbol": "600519.SH", "name": "贵州茅台"}}。
    """
    if not query or not str(query).strip():
        return {"ok": False, "error": "查询内容为空", "data": None}
    raw = str(query).strip()
    try:
        details = all_stock_details()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"无法加载股票代码表：{e}", "data": None}

    if details.empty:
        return {"ok": False, "error": "股票代码表为空", "data": None}

    code_map: dict[str, pd.Series] = {}
    name_map: dict[str, pd.Series] = {}
    for _, row in details.iterrows():
        sym = str(row.get("symbol") or "")
        code = sym.split(".")[0] if "." in sym else sym
        nm = str(row.get("name") or "")
        code_map[sym] = row
        code_map[code] = row
        if nm:
            name_map[nm] = row

    normalized = re.sub(r"\s+", "", raw)
    m = re.search(r"(\d{6})", normalized)
    if m:
        code = m.group(1)
        row = None
        for key in (code, f"{code}.SH", f"{code}.SZ"):
            if key in code_map:
                row = code_map[key]
                break
        if row is not None:
            sym = str(row["symbol"])
            return {"ok": True, "data": {"symbol": sym, "name": str(row.get("name") or sym)}}

    exact = name_map.get(normalized)
    if exact is not None:
        sym = str(exact["symbol"])
        return {"ok": True, "data": {"symbol": sym, "name": str(exact.get("name") or sym)}}

    for nm, row in name_map.items():
        if normalized in nm:
            sym = str(row["symbol"])
            return {"ok": True, "data": {"symbol": sym, "name": str(row.get("name") or sym)}}

    return {"ok": False, "error": f"未找到匹配的股票：{query}", "data": None}


def get_stock_details(symbols: list[str] | str, fields: list[str] | None = None) -> pd.DataFrame:
    key = f"pd:detail:{','.join(symbols) if isinstance(symbols, list) else symbols}"

    def producer() -> pd.DataFrame:
        df = pdapi.get_stock_detail(symbol=symbols, fields=fields or [], status=1)
        return _to_frame(df)

    return _call(key, ttl=60 * 60 * 12, fn=producer)


# --------------------------- 交易日工具 --------------------------- #
def latest_trade_date() -> str:
    """最近一个已完成交易日 YYYYMMDD。"""
    end = _today()
    start = (date.today() - timedelta(days=15)).strftime("%Y%m%d")

    def producer() -> str:
        df = pdapi.get_trade_cal(
            start_date=start,
            end_date=end,
            exchange="SH",
            is_trading_day=1,
            fields=["nature_date"],
        )
        df = _to_frame(df)
        if df.empty or "nature_date" not in df.columns:
            raise RuntimeError("交易日历为空")
        return str(df["nature_date"].astype(str).max())

    return _call(f"pd:last_trade:{end}", ttl=60 * 30, fn=producer)


def get_trade_cal(start_date: str, end_date: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_trade_cal(
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            exchange="SH",
            is_trading_day=1,
            fields=["nature_date"],
        )
        return _to_frame(df)

    return _call(f"pd:trade_cal:{start_date}:{end_date}", ttl=60 * 60 * 24, fn=producer)


# --------------------------- 行情 --------------------------- #
def get_stock_daily(
    symbol: str | list[str],
    start_date: str,
    end_date: str,
    fields: list[str] | None = None,
) -> pd.DataFrame:
    key = f"pd:daily:{symbol}:{start_date}:{end_date}"

    def producer() -> pd.DataFrame:
        df = pdapi.get_stock_daily(
            symbol=symbol,
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            fields=fields or [],
            st=True,
        )
        df = _to_frame(df)
        if not df.empty and "date" in df.columns:
            df = df.sort_values("date").reset_index(drop=True)
        return df

    return _call(key, ttl=60 * 30, fn=producer)


def get_daily_kline(symbol: str, days: int = 250) -> pd.DataFrame:
    """个股前复权日线（供技术指标使用），返回升序。"""
    end = latest_trade_date()
    start = (date.today() - timedelta(days=int(days * 1.8) + 30)).strftime("%Y%m%d")

    def producer() -> pd.DataFrame:
        df = pdapi.get_stock_daily_pre(
            symbol=symbol,
            start_date=start,
            end_date=end,
            fields=["symbol", "date", "open", "close", "high", "low", "volume", "name"],
            st=True,
        )
        df = _to_frame(df)
        if not df.empty and "date" in df.columns:
            df = df.sort_values("date").reset_index(drop=True).tail(days)
        return df

    return _call(f"pd:kline:{symbol}:{days}", ttl=60 * 30, fn=producer)


def get_index_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_index_daily(
            symbol=[symbol],
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            fields=[],
        )
        df = _to_frame(df)
        if not df.empty and "date" in df.columns:
            df = df.sort_values("date").reset_index(drop=True)
        return df

    return _call(f"pd:index_daily:{symbol}:{start_date}:{end_date}", ttl=60 * 30, fn=producer)


def get_index_indicator(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_index_indicator(
            symbol=[symbol],
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            fields=[],
        )
        df = _to_frame(df)
        if not df.empty and "date" in df.columns:
            df = df.sort_values("date").reset_index(drop=True)
        return df

    return _call(f"pd:index_ind:{symbol}:{start_date}:{end_date}", ttl=60 * 30, fn=producer)


def get_factor(index: str, trade_date: str, factors: list[str]) -> pd.DataFrame:
    """取某个指数股票池在指定交易日的基础因子（市值/换手/成交额等）。"""
    def producer() -> pd.DataFrame:
        df = pdapi.get_factor(
            symbol="",
            start_date=_normalize_date(trade_date),
            end_date=_normalize_date(trade_date),
            factors=factors,
            index_component=index,
            type="stock",
        )
        df = _to_frame(df)
        if not df.empty and "date" in df.columns:
            df = df.sort_values("symbol").reset_index(drop=True)
        return df

    return _call(f"pd:factor:{index}:{trade_date}", ttl=60 * 30, fn=producer)


# --------------------------- 基本面 --------------------------- #
FINA_FIELDS = [
    "symbol",
    "quarter",
    "date",
    "is_revenue",
    "is_gross_profit",
    "is_operate_profit",
    "is_n_income_attr_p",
    "is_net_after_nr",
    "cfs_net_cash_operating",
    "bs_total_assets",
    "bs_total_hldr_eqy_exc_min_int",
    "bs_total_hldr_eqy_inc_min_int",
    "bs_total_liab",
    "is_basic_eps",
    "is_diluted_eps",
    "is_rd_exp",
    "bs_cap_stk",
    "if_adjusted",
]


def get_fina_reports(
    symbol: str | list[str],
    start_quarter: str,
    end_quarter: str,
    fields: list[str] | None = None,
) -> pd.DataFrame:
    """财务季度报告。按 symbol + quarter 保留最新披露日，避免重复修订记录。"""
    sym_key = ",".join(symbol) if isinstance(symbol, list) else str(symbol)
    key = f"pd:fina:{sym_key}:{start_quarter}:{end_quarter}"

    def producer() -> pd.DataFrame:
        df = pdapi.get_fina_reports(
            symbol=symbol,
            start_quarter=start_quarter,
            end_quarter=end_quarter,
            is_latest=False,
            fields=fields or FINA_FIELDS,
        )
        df = _to_frame(df)
        if df.empty:
            return df
        if "date" in df.columns and "quarter" in df.columns and "symbol" in df.columns:
            df["date"] = df["date"].astype(str)
            df = df.sort_values("date").groupby(["symbol", "quarter"], as_index=False).tail(1)
        return df.reset_index(drop=True)

    return _call(key, ttl=60 * 60 * 6, fn=producer)


def get_fina_performance(
    symbol: str | list[str],
    info_date: str,
    fields: list[str] | None = None,
) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_fina_performance(
            symbol=symbol,
            info_date=_normalize_date(info_date),
            fields=fields or [],
        )
        return _to_frame(df)

    sym_key = ",".join(symbol) if isinstance(symbol, list) else str(symbol)
    return _call(f"pd:fina_perf:{sym_key}:{info_date}", ttl=60 * 60 * 6, fn=producer)


def get_audit_opinion(symbol: str | list[str], start_quarter: str, end_quarter: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_audit_opinion(
            symbol=symbol,
            start_quarter=start_quarter,
            end_quarter=end_quarter,
            market="cn",
            fields=[],
        )
        return _to_frame(df)

    sym_key = ",".join(symbol) if isinstance(symbol, list) else str(symbol)
    return _call(f"pd:audit:{sym_key}:{start_quarter}:{end_quarter}", ttl=60 * 60 * 12, fn=producer)


# --------------------------- 分红 / 回购 / 股东 --------------------------- #
def get_cash_dividend(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_stock_cash_dividend(
            symbol=symbol,
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            market="cn",
            fields=[],
        )
        return _to_frame(df)

    return _call(f"pd:div:{symbol}:{start_date}:{end_date}", ttl=60 * 60 * 12, fn=producer)


def get_dividend(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_stock_dividend(
            symbol=symbol,
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            market="cn",
            fields=[],
        )
        return _to_frame(df)

    return _call(f"pd:div_type:{symbol}:{start_date}:{end_date}", ttl=60 * 60 * 12, fn=producer)


def get_repurchase(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_repurchase(
            symbol=symbol,
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            fields=[],
        )
        return _to_frame(df)

    return _call(f"pd:buyback:{symbol}:{start_date}:{end_date}", ttl=60 * 60 * 12, fn=producer)


def get_top_holders(symbol: str, start_date: str, end_date: str, top_n: int = 10) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_top_holders(
            symbol=symbol,
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            start_rank=1,
            end_rank=top_n,
            stock_type="flow",
            market="cn",
            fields=[],
        )
        return _to_frame(df)

    return _call(f"pd:holders:{symbol}:{start_date}:{end_date}:{top_n}", ttl=60 * 60 * 12, fn=producer)


def get_holder_count(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_holder_count(
            symbol=symbol,
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            fields=[],
        )
        return _to_frame(df)

    return _call(f"pd:holder_count:{symbol}:{start_date}:{end_date}", ttl=60 * 60 * 12, fn=producer)


def get_shareholder_change(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_stock_shareholder_change(
            symbol=symbol,
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            fields=[],
        )
        return _to_frame(df)

    return _call(f"pd:share_change:{symbol}:{start_date}:{end_date}", ttl=60 * 60 * 12, fn=producer)


def get_pledge(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_stock_pledge(
            symbol=symbol,
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            fields=[],
        )
        return _to_frame(df)

    return _call(f"pd:pledge:{symbol}:{start_date}:{end_date}", ttl=60 * 60 * 12, fn=producer)


def get_margin(symbol: str | list[str], start_date: str, end_date: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_margin(
            symbol=symbol,
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            margin_type="cash",
            fields=[],
        )
        return _to_frame(df)

    sym_key = ",".join(symbol) if isinstance(symbol, list) else str(symbol)
    return _call(f"pd:margin:{sym_key}:{start_date}:{end_date}", ttl=60 * 30, fn=producer)


def get_hsgt_hold(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    def producer() -> pd.DataFrame:
        df = pdapi.get_hsgt_hold(
            symbol=symbol,
            start_date=_normalize_date(start_date),
            end_date=_normalize_date(end_date),
            fields=[],
        )
        return _to_frame(df)

    return _call(f"pd:hsgt:{symbol}:{start_date}:{end_date}", ttl=60 * 60 * 6, fn=producer)


# --------------------------- 行业 --------------------------- #
def get_stock_industry(symbol: str) -> dict:
    def producer() -> dict:
        df = pdapi.get_stock_industry(stock_symbol=symbol, level="L1")
        df = _to_frame(df)
        if df.empty:
            return {}
        row = df.iloc[0]
        return {
            "code": str(row.get("industry_code") or ""),
            "name": str(row.get("industry_name") or ""),
        }

    return _call(f"pd:ind:{symbol}", ttl=60 * 60 * 24, fn=producer)


def get_industry_map() -> dict[str, str]:
    """申万 L1 行业映射：股票代码 -> 行业名称。"""
    def producer() -> dict[str, str]:
        df = pdapi.get_industry_constituents(
            industry_code="",
            stock_symbol="",
            level="L1",
            fields=["stock_symbol", "l1_name"],
        )
        df = _to_frame(df)
        out: dict[str, str] = {}
        for _, row in df.iterrows():
            out[str(row.get("stock_symbol") or "")] = str(row.get("l1_name") or "")
        return out

    return _call("pd:industry_map", ttl=60 * 60 * 24, fn=producer)


# --------------------------- 手动验证入口 --------------------------- #
if __name__ == "__main__":
    ensure_token()
    print("latest_trade_date:", latest_trade_date())
    print("resolve:", resolve_symbol("贵州茅台"))
    print("resolve:", resolve_symbol("000858"))
    k = get_daily_kline("600519.SH", days=5)
    print("kline:", k[["date", "close"]].to_string(index=False) if not k.empty else "empty")
    f = get_fina_reports("600519.SH", "2023q4", "2025q4")
    print("fina:", f[["symbol", "quarter", "is_revenue", "is_n_income_attr_p"]].tail(3).to_string(index=False))
