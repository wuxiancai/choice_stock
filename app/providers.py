from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta

from .config import settings


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class SectorFetchResult:
    rows: list[dict]
    source: str
    fallback_errors: list[str]


_PROXY_ENV_KEYS = ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy")


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
            flow_map = {} if money is None else {r.ts_code: float((r.buy_elg_amount or 0) - (r.sell_elg_amount or 0)) * 1000 for r in money.itertuples()}
        except Exception:
            # Tushare 权限不足时不臆造个股主力资金，保留为 0 并由运行记录可见。
            flow_map = {}
        if name_map is None:
            names = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name")
            name_map = dict(zip(names.ts_code, names.name))
        if not include_basics:
            basic_map = {}
        else:
            try:
                basics = pro.daily_basic(
                    trade_date=trade_date,
                    fields="ts_code,turnover_rate,volume_ratio,pe,pb,total_mv",
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
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "pct_chg": r.pct_chg, "vol": r.vol, "amount": r.amount,
            "main_net_inflow": flow_map.get(r.ts_code, 0),
            "turnover_rate": number(basic_map.get(r.ts_code), "turnover_rate"),
            "volume_ratio": number(basic_map.get(r.ts_code), "volume_ratio"),
            "total_mv": number(basic_map.get(r.ts_code), "total_mv"),
            "pe": number(basic_map.get(r.ts_code), "pe"),
            "pb": number(basic_map.get(r.ts_code), "pb"),
        } for r in frame.itertuples()]
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"拉取 Tushare 日线失败：{exc}") from exc


def _fetch_sector_frame():
    import akshare as ak
    return ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")


def _fetch_tushare_ths_sector_frame(trade_date: str):
    return _ts().moneyflow_ind_ths(trade_date=trade_date)


def _fetch_tushare_dc_sector_frame(trade_date: str):
    return _ts().moneyflow_ind_dc(trade_date=trade_date)


def _fetch_ths_sector_frame():
    import akshare as ak
    return ak.stock_fund_flow_industry(symbol="即时")


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
    for raw in frame.to_dict(orient="records"):
        name = _first_value(raw, "name", "行业", "行业名称")
        if not name:
            continue
        # 同花顺网页表头为“净额(亿)”，但 pandas 解析后的值不再保留“亿”后缀。
        net_multiplier = 100_000_000 if source == "ths" else 1
        net_amount = _as_number(
            _first_value(raw, "net_amount", "净额", "今日主力净流入_净额"), net_multiplier,
        )
        rows.append({
            "trade_date": trade_date, "sector_code": str(name), "sector_name": str(name),
            "pct_chg": _as_number(_first_value(raw, "pct_change", "pct_chg", "行业-涨跌幅", "今日涨跌幅")),
            "amount": net_amount, "main_net_inflow": net_amount, "source": source,
        })
    if not rows:
        raise ProviderError(f"{source} 未返回有效行业资金流数据")
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
    """按 Tushare、东方财富、同花顺多源顺序取得真实行业资金流。"""
    providers = (
        ("tushare_ths", lambda: _fetch_tushare_ths_sector_frame(trade_date)),
        ("tushare_dc", lambda: _fetch_tushare_dc_sector_frame(trade_date)),
        ("eastmoney", _fetch_eastmoney_sector_frame_with_retry),
        ("ths", _fetch_ths_sector_frame),
    )
    failures = []
    for source, fetch_frame in providers:
        try:
            return SectorFetchResult(_normalize_sector_rows(fetch_frame(), trade_date, source), source, failures)
        except Exception as exc:
            failures.append(f"{source}: {exc}")
    raise ProviderError("行业资金流所有来源均失败：" + "；".join(failures))
