from __future__ import annotations

import os
import time
from math import isfinite
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

from .config import settings


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class SectorFetchResult:
    rows: list[dict]
    source: str
    fallback_errors: list[str]


_PROXY_ENV_KEYS = ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy")
MIN_VALID_SECTOR_ROWS = 30
# Tushare's public permission table confirms every endpoint called in this module
# is available to an account with 3,000 points or fewer.  Keep this declaration
# next to the calls so a future provider addition cannot silently reintroduce a
# higher-tier endpoint.
TUSHARE_3000_POINT_APIS = frozenset({"trade_cal", "daily", "daily_basic", "moneyflow", "stock_basic"})
_EASTMONEY_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://data.eastmoney.com/bkzj/hy.html",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}
_EASTMONEY_INDUSTRY_RANK_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_EASTMONEY_FLOW_HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
_EASTMONEY_PRICE_HISTORY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def tushare_main_net_inflow(row) -> float:
    """Return the conventional main-force net inflow in yuan.

    Tushare's ``moneyflow`` order-value fields are expressed in 万元.  The
    dashboard's "主力" definition is 大单 + 特大单, rather than only 特大单 or
    Tushare's separate all-order ``net_mf_amount`` field.
    """
    def amount(field: str) -> float:
        value = getattr(row, field, None)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.0
        return numeric if isfinite(numeric) else 0.0

    return (
        amount("buy_lg_amount") - amount("sell_lg_amount")
        + amount("buy_elg_amount") - amount("sell_elg_amount")
    ) * 10_000


@contextmanager
def without_http_proxy():
    """Temporarily bypass a broken local proxy for a public-data retry."""
    previous = {key: os.environ.pop(key) for key in _PROXY_ENV_KEYS if key in os.environ}
    try:
        yield
    finally:
        os.environ.update(previous)


def _ts():
    if not settings.tushare_token or settings.tushare_token.startswith("replace-"):
        raise ProviderError("未配置 TUSHARE_TOKEN；请在 .env 设置，密钥不会显示在页面或日志中")
    try:
        import tushare as ts
        ts.set_token(settings.tushare_token)
        return ts.pro_api()
    except Exception as exc:
        raise ProviderError(f"Tushare 初始化失败：{exc}") from exc


def recent_trade_dates(days: int = 90) -> list[str]:
    if days < 1:
        raise ValueError("交易日数量必须至少为 1")
    pro = _ts()
    end = date.today()
    start = end - timedelta(days=max(days * 3, 30))
    try:
        calendar = pro.trade_cal(exchange="SSE", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), is_open="1")
        if calendar.empty:
            raise ProviderError("Tushare 未返回近期开市日")
        dates = sorted(str(value) for value in calendar["cal_date"])
        if len(dates) < days:
            raise ProviderError(f"Tushare 仅返回 {len(dates)} 个开市日，少于要求的 {days} 个")
        return dates[-days:]
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"读取交易日历失败：{exc}") from exc


def latest_trade_date() -> str:
    return recent_trade_dates(1)[0]


def fetch_quotes(
    trade_date: str, *, name_map: dict[str, str] | None = None, include_moneyflow: bool = True,
    include_basics: bool = True,
) -> list[dict]:
    pro = _ts()
    try:
        frame = pro.daily(trade_date=trade_date)
        if frame.empty:
            raise ProviderError(f"{trade_date} 无日线数据（可能尚未收盘或无权限）")
        try:
            money = pro.moneyflow(trade_date=trade_date) if include_moneyflow else None
            flow_map = {} if money is None else {
                row.ts_code: tushare_main_net_inflow(row) for row in money.itertuples()
            }
        except Exception:
            # Tushare 权限不足时不臆造个股主力资金，保留为 0 并由运行记录可见。
            flow_map = {}
        if name_map is None:
            names = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")
            name_map = dict(zip(names.ts_code, names.name))
            industry_map = dict(zip(names.ts_code, names.industry))
        else:
            industry_map = {}
        if not include_basics:
            basic_map = {}
        else:
            try:
                basics = pro.daily_basic(
                    trade_date=trade_date,
                    fields="ts_code,turnover_rate,volume_ratio,pe_ttm,pb,total_mv",
                )
                basic_map = {r.ts_code: r for r in basics.itertuples()}
            except Exception:
                # 基础估值数据权限不足时保留为空，页面会显式展示缺失值。
                basic_map = {}

        def number(row, field: str):
            value = getattr(row, field, None) if row is not None else None
            return None if value is None else float(value)

        return [{
            "trade_date": trade_date, "ts_code": r.ts_code, "name": name_map.get(r.ts_code, r.ts_code),
            "industry": industry_map.get(r.ts_code),
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "pct_chg": r.pct_chg, "vol": r.vol, "amount": r.amount,
            "main_net_inflow": flow_map.get(r.ts_code, 0),
            "turnover_rate": number(basic_map.get(r.ts_code), "turnover_rate"),
            "volume_ratio": number(basic_map.get(r.ts_code), "volume_ratio"),
            "total_mv": number(basic_map.get(r.ts_code), "total_mv"),
            # The dashboard's persisted ``pe`` column is explicitly dynamic PE
            # (TTM), not Tushare's static ``pe`` field.
            "pe": number(basic_map.get(r.ts_code), "pe_ttm"),
            "pb": number(basic_map.get(r.ts_code), "pb"),
        } for r in frame.itertuples()]
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"拉取 Tushare 日线失败：{exc}") from exc


def _fetch_sector_frame():
    return _fetch_eastmoney_industry_rows()


def _eastmoney_json(url: str, params: dict) -> dict:
    """Fetch Eastmoney JSON with the headers and proxy modes its endpoints require."""
    import requests

    errors = []
    # FastLink can intermittently reset either proxied or direct HTTPS requests.
    # Retrying both modes is more reliable than mutating global proxy variables,
    # especially while history backfill is concurrent.
    for trust_env in (True, False, True):
        try:
            with requests.Session() as session:
                session.trust_env = trust_env
                response = session.get(url, params=params, headers=_EASTMONEY_HEADERS, timeout=20)
                response.raise_for_status()
                payload = response.json()
            if payload.get("rc") not in (0, None) or not isinstance(payload.get("data"), dict):
                raise ProviderError(f"东方财富返回异常 rc={payload.get('rc')}")
            return payload
        except Exception as exc:
            errors.append(str(exc))
            time.sleep(0.2)
    raise ProviderError(f"东方财富请求失败（已重试代理与直连：{errors[-1]}）")


def _fetch_eastmoney_industry_rows() -> list[dict]:
    payload = _eastmoney_json(_EASTMONEY_INDUSTRY_RANK_URL, {
        "fid": "f62", "po": "1", "pz": "100", "pn": "1", "np": "1", "fltt": "2", "invt": "2",
        "ut": "8dec03ba335b81bf4ebdf7b29ec27d15", "fs": "m:90 t:2",
        "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124,f1,f13",
    })
    rows = payload["data"].get("diff") or []
    if not isinstance(rows, list) or len(rows) < MIN_VALID_SECTOR_ROWS:
        raise ProviderError(f"东方财富仅返回 {len(rows) if isinstance(rows, list) else 0} 条行业数据")
    return rows


@lru_cache(maxsize=1)
def _eastmoney_industry_codes() -> dict[str, str]:
    return {
        str(row["f14"]): str(row["f12"])
        for row in _fetch_eastmoney_industry_rows()
        if row.get("f14") and row.get("f12")
    }


def _fetch_ths_sector_frame():
    import akshare as ak
    return ak.stock_fund_flow_industry(symbol="即时")


def _fetch_tencent_sector_rows(trade_date: str) -> list[dict]:
    """Aggregate Tencent's live stock flow feed by Tushare's industry classification."""
    import akshare as ak

    def fetch_frame():
        return ak.stock_zh_a_spot_tx()

    try:
        frame = fetch_frame()
    except Exception as exc:
        if "proxy" not in str(exc).lower():
            raise
        with without_http_proxy():
            frame = fetch_frame()
    try:
        basics = _ts().stock_basic(exchange="", list_status="L", fields="ts_code,industry")
    except Exception as exc:
        raise ProviderError(f"腾讯行情已返回，但行业分类读取失败：{exc}") from exc

    industry_by_code = dict(zip(basics["ts_code"], basics["industry"]))

    def to_ts_code(code: str) -> str | None:
        raw = str(code).lower()
        if len(raw) < 3:
            return None
        exchange, number = raw[:2], raw[2:]
        return f"{number}.{exchange.upper()}" if exchange in {"sh", "sz", "bj"} else None

    grouped: dict[str, dict[str, float]] = {}
    for raw in frame.to_dict(orient="records"):
        ts_code = to_ts_code(raw.get("code", ""))
        industry = industry_by_code.get(ts_code) if ts_code else None
        pct_chg = _as_number(raw.get("zdf"))
        net_inflow = _as_number(raw.get("zljlr"), 10_000)  # Tencent reports this field in 万元.
        turnover = _as_number(raw.get("turnover"), 10_000) or 0
        if not industry or pct_chg is None or net_inflow is None:
            continue
        aggregate = grouped.setdefault(str(industry), {"net_inflow": 0, "weighted_change": 0, "weight": 0, "count": 0})
        weight = turnover if turnover > 0 else 1
        aggregate["net_inflow"] += net_inflow
        aggregate["weighted_change"] += pct_chg * weight
        aggregate["weight"] += weight
        aggregate["count"] += 1

    rows = [
        {
            "trade_date": trade_date, "sector_code": industry, "sector_name": industry,
            "pct_chg": values["weighted_change"] / values["weight"],
            "amount": values["net_inflow"], "main_net_inflow": values["net_inflow"], "source": "tencent",
        }
        for industry, values in grouped.items() if values["count"] > 0 and values["weight"] > 0
    ]
    if len(rows) < MIN_VALID_SECTOR_ROWS:
        raise ProviderError(f"腾讯仅聚合到 {len(rows)} 个有效行业，少于完整性下限 {MIN_VALID_SECTOR_ROWS}")
    return rows


def _fetch_eastmoney_sector_history(sector_code: str, start_date: str, end_date: str) -> tuple[list[str], list[str]]:
    """Fetch one Eastmoney board by code, avoiding AKShare's fragile name lookup."""
    flow = _eastmoney_json(_EASTMONEY_FLOW_HISTORY_URL, {
        "lmt": "0", "klt": "101", "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "secid": f"90.{sector_code}", "ut": "b2884a393a59ad64002292a3e90d46a5",
    })
    price = _eastmoney_json(_EASTMONEY_PRICE_HISTORY_URL, {
        "secid": f"90.{sector_code}", "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61", "klt": "101", "fqt": "0",
        "beg": start_date, "end": end_date, "smplmt": "10000", "lmt": "1000000",
    })
    flow_rows = flow["data"].get("klines") or []
    price_rows = price["data"].get("klines") or []
    if not flow_rows or not price_rows:
        raise ProviderError(f"东方财富行业 {sector_code} 未返回完整历史数据")
    return flow_rows, price_rows


def _as_number(value, multiplier: float = 1):
    if value is None or str(value).strip() in {"", "--", "-"}:
        return None
    text = str(value).replace(",", "").strip()
    if text.endswith("亿"):
        text, multiplier = text[:-1], 100_000_000
    elif text.endswith("万"):
        text, multiplier = text[:-1], 10_000
    elif text.endswith("元"):
        text = text[:-1]
    return float(text.rstrip("%")) * multiplier


def _first_value(row: dict, *keys: str):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _normalize_sector_rows(frame, trade_date: str, source: str) -> list[dict]:
    rows = []
    records = frame if isinstance(frame, list) else frame.to_dict(orient="records")
    for raw in records:
        name = _first_value(raw, "name", "行业", "行业名称", "f14", "名称")
        if not name:
            continue
        # 同花顺网页表头为“净额(亿)”，但 pandas 解析后的值不再保留“亿”后缀。
        net_multiplier = 100_000_000 if source == "ths" else 1
        pct_chg = _as_number(_first_value(raw, "pct_change", "pct_chg", "行业-涨跌幅", "今日涨跌幅"))
        net_amount = _as_number(
            _first_value(raw, "net_amount", "净额", "今日主力净流入_净额"), net_multiplier,
        )
        if pct_chg is None or net_amount is None:
            continue
        rows.append({
            "trade_date": trade_date, "sector_code": str(_first_value(raw, "sector_code", "code", "f12", "代码") or name), "sector_name": str(name),
            "pct_chg": pct_chg,
            "amount": net_amount, "main_net_inflow": net_amount, "source": source,
        })
    if len(rows) < MIN_VALID_SECTOR_ROWS:
        raise ProviderError(f"{source} 仅返回 {len(rows)} 条有效行业资金流数据，少于完整性下限 {MIN_VALID_SECTOR_ROWS}")
    return rows


def _fetch_eastmoney_sector_frame_with_retry():
    try:
        return _fetch_sector_frame()
    except Exception as exc:
        if "proxy" not in str(exc).lower():
            raise
        try:
            with without_http_proxy():
                return _fetch_sector_frame()
        except Exception as direct_exc:
            raise ProviderError(f"代理请求失败（{exc}）；直连重试失败（{direct_exc}）") from direct_exc


def fetch_sectors(trade_date: str) -> SectorFetchResult:
    """Use permitted Tushare per-stock aggregation before public live fallbacks."""
    providers = (
        ("tushare_moneyflow", lambda: _fetch_tushare_sector_rows(trade_date)),
        ("eastmoney", _fetch_eastmoney_sector_frame_with_retry),
        ("ths", _fetch_ths_sector_frame),
        ("tencent", lambda: _fetch_tencent_sector_rows(trade_date)),
    )
    failures = []
    for source, fetch_frame in providers:
        try:
            rows = fetch_frame()
            if source in {"tushare_moneyflow", "tencent"}:
                return SectorFetchResult(rows, source, failures)
            return SectorFetchResult(_normalize_sector_rows(rows, trade_date, source), source, failures)
        except Exception as exc:
            failures.append(f"{source}: {exc}")
    def concise_failure(failure: str) -> str:
        source, _, detail = failure.partition(": ")
        lowered = detail.lower()
        if "无接口" in detail or "无权限" in detail or "access permission" in lowered:
            detail = "无接口访问权限"
        elif "expecting value" in lowered or "json" in lowered:
            detail = "返回无效数据"
        elif "no tables found" in lowered:
            detail = "未返回行业表"
        elif len(detail) > 80:
            detail = detail[:77] + "..."
        return f"{source}: {detail}"

    raise ProviderError("行业资金流所有来源均失败（" + "；".join(concise_failure(item) for item in failures) + "）")


def fetch_tushare_sector_history(trade_dates: list[str]) -> tuple[list[dict], list[str], list[str]]:
    """Aggregate permitted Tushare per-stock flow into dated industry snapshots."""
    if not trade_dates:
        return [], [], []
    pro = _ts()
    try:
        basics = pro.stock_basic(exchange="", list_status="L", fields="ts_code,industry")
        industry_by_code = dict(zip(basics["ts_code"], basics["industry"]))
    except Exception as exc:
        return [], list(trade_dates), [f"Tushare 个股资金流行业聚合：行业分类读取失败（{exc}）"]

    rows, unresolved_dates, diagnostics = [], [], []
    for trade_date in trade_dates:
        try:
            flow_frame = pro.moneyflow(trade_date=trade_date)
            quote_frame = pro.daily(trade_date=trade_date)
            quote_by_code = {
                item.ts_code: (float(item.pct_chg), float(item.amount))
                for item in quote_frame.itertuples()
                if item.pct_chg is not None and item.amount is not None
            }
            grouped: dict[str, dict[str, float]] = {}
            for item in flow_frame.itertuples():
                industry = industry_by_code.get(item.ts_code)
                quote = quote_by_code.get(item.ts_code)
                if not industry or quote is None:
                    continue
                net_inflow = tushare_main_net_inflow(item)
                pct_chg, amount = quote
                aggregate = grouped.setdefault(str(industry), {"net_inflow": 0, "weighted_change": 0, "weight": 0})
                weight = amount if amount > 0 else 1
                aggregate["net_inflow"] += net_inflow
                aggregate["weighted_change"] += pct_chg * weight
                aggregate["weight"] += weight
            date_rows = [
                {
                    "trade_date": trade_date, "sector_code": industry, "sector_name": industry,
                    "pct_chg": values["weighted_change"] / values["weight"],
                    "amount": values["net_inflow"], "main_net_inflow": values["net_inflow"],
                    "source": "tushare_moneyflow_aggregate",
                }
                for industry, values in grouped.items() if values["weight"] > 0
            ]
            if len(date_rows) < MIN_VALID_SECTOR_ROWS:
                raise ProviderError(f"仅聚合到 {len(date_rows)} 个有效行业")
            rows.extend(date_rows)
            diagnostics.append(f"Tushare 个股资金流行业聚合：{trade_date} 成功（{len(date_rows)} 个行业）")
        except Exception as exc:
            unresolved_dates.append(trade_date)
            diagnostics.append(f"Tushare 个股资金流行业聚合：{trade_date} 失败（{str(exc)[:120]}）")
    return rows, unresolved_dates, diagnostics


def _fetch_tushare_sector_rows(trade_date: str) -> list[dict]:
    rows, unresolved_dates, diagnostics = fetch_tushare_sector_history([trade_date])
    if unresolved_dates:
        raise ProviderError(diagnostics[-1] if diagnostics else f"{trade_date} 行业聚合失败")
    return rows


def fetch_sector_history(trade_dates: list[str], sector_names: list[str]) -> tuple[list[dict], list[str]]:
    """Retrieve dated industry snapshots using permitted Tushare data then Eastmoney.

    The aggregation uses Tushare ``moneyflow`` (per-stock flow, 2,000-point tier),
    not either restricted Tushare industry-flow API. THS and Tencent are live-only.
    """
    if not trade_dates or not sector_names:
        return [], []
    rows, unresolved_dates, diagnostics = fetch_tushare_sector_history(trade_dates)
    if not unresolved_dates:
        diagnostics.append("同花顺、腾讯：仅支持当日快照，历史回填未使用；Tushare 行业资金流接口超出 3000 积分权限，未调用")
        return rows, diagnostics
    wanted_dates = set(unresolved_dates)
    start_date, end_date = min(unresolved_dates), max(unresolved_dates)

    def normalize(symbol: str, sector_code: str, flow_rows: list[str], price_rows: list[str]) -> list[dict]:
        flows = {
            row.split(",")[0].replace("-", ""): _as_number(row.split(",")[1])
            for row in flow_rows if len(row.split(",")) > 1
        }
        changes = {
            row.split(",")[0].replace("-", ""): _as_number(row.split(",")[8])
            for row in price_rows if len(row.split(",")) > 8
        }
        rows = []
        for trade_date in sorted(wanted_dates):
            net_inflow, pct_chg = flows.get(trade_date), changes.get(trade_date)
            if net_inflow is None or pct_chg is None:
                continue
            rows.append({
                "trade_date": trade_date, "sector_code": sector_code, "sector_name": symbol,
                "pct_chg": pct_chg, "amount": net_inflow, "main_net_inflow": net_inflow,
                "source": "eastmoney_history",
            })
        return rows

    failures = []
    names = sorted(set(sector_names))
    codes = _eastmoney_industry_codes()
    sectors = [(name, codes[name]) for name in names if name in codes]
    failures = [f"{name}: 东方财富行业代码不存在" for name in names if name not in codes]
    if not sectors:
        diagnostics.append(f"东方财富行业历史：指定 {len(unresolved_dates)} 日，成功 0/{len(names)} 个行业，失败 {len(failures)} 个行业")
        diagnostics.append("同花顺、腾讯：仅支持当日快照，历史回填未使用；Tushare 行业资金流超出 3000 积分权限，未调用")
        return rows, diagnostics
    first_sector, remaining_sectors = sectors[0], sectors[1:]
    try:
        rows.extend(normalize(first_sector[0], first_sector[1], *_fetch_eastmoney_sector_history(first_sector[1], start_date, end_date)))
    except Exception as exc:
        failures.append(f"{first_sector[0]}: {exc}")
    # Keep public history requests deliberately low-concurrency after the code map warmup.
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(lambda item: normalize(item[0], item[1], *_fetch_eastmoney_sector_history(item[1], start_date, end_date)), item): item
            for item in remaining_sectors
        }
        for future in as_completed(futures):
            name, _ = futures[future]
            try:
                rows.extend(future.result())
            except Exception as exc:
                failures.append(f"{name}: {exc}")
    eastmoney_successes = len({row["sector_name"] for row in rows if row["trade_date"] in wanted_dates and row["source"] == "eastmoney_history"})
    diagnostics.append(
        f"东方财富行业历史：指定 {len(unresolved_dates)} 日，成功 {eastmoney_successes}/{len(names)} 个行业，失败 {len(failures)} 个行业"
    )
    diagnostics.append("同花顺、腾讯：仅支持当日快照，历史回填未使用；Tushare 行业资金流超出 3000 积分权限，未调用")
    # Individual industry exceptions can contain long provider URLs and are not useful
    # in the dashboard.  The per-source summary above is actionable and keeps the
    # persistent run status readable.
    return rows, diagnostics
