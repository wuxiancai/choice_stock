from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, timedelta

from .config import settings


class ProviderError(RuntimeError):
    pass


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


def fetch_sectors(trade_date: str) -> list[dict]:
    """东方财富行业资金流公开接口，经 AKShare 封装；失败不影响日线信号。"""
    try:
        try:
            frame = _fetch_sector_frame()
        except Exception as exc:
            if "proxy" not in str(exc).lower():
                raise
            try:
                with without_http_proxy():
                    frame = _fetch_sector_frame()
            except Exception as direct_exc:
                raise ProviderError(
                    f"东方财富行业资金流拉取失败：代理请求失败（{exc}）；直连重试失败（{direct_exc}）"
                ) from direct_exc
        rows = []
        for r in frame.itertuples():
            name = str(getattr(r, "行业", getattr(r, "名称", "未知板块")))
            rows.append({"trade_date": trade_date, "sector_code": name, "sector_name": name,
                         "pct_chg": float(getattr(r, "今日涨跌幅", 0) or 0),
                         "amount": float(getattr(r, "今日主力净流入_净额", 0) or 0),
                         "main_net_inflow": float(getattr(r, "今日主力净流入_净额", 0) or 0)})
        return rows
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"东方财富行业资金流拉取失败：{exc}") from exc
