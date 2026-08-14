from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import settings
from .database import connect, initialize
from .indicators import calculate
from .providers import ProviderError, fetch_quotes, fetch_sectors, recent_trade_dates


FILTER_METRICS = (
    "score", "macd", "kdj_j", "rsi14", "boll_position", "nine_turn", "main_net_inflow",
    "volume_ratio", "turnover_rate", "amount", "total_mv", "pe", "pb", "pct_chg",
)


def record_system_error(source: str, error: Exception | str) -> None:
    """Persist a concise, browser-safe runtime error without exposing configured secrets."""
    message = str(error)
    if settings.tushare_token:
        message = message.replace(settings.tushare_token, "[REDACTED]")
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO system_logs(created_at,level,source,message) VALUES (?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), "ERROR", source, message[:2000]),
            )
    except Exception:
        # 日志落库失败不能覆盖原始运行错误。
        pass


def recent_system_errors(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT created_at,level,source,message FROM system_logs WHERE level='ERROR' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def normalize_signal_filters(raw_filters: dict[str, str]) -> dict[str, float]:
    filters = {}
    for metric in FILTER_METRICS:
        for bound in ("min", "max"):
            key = f"{bound}_{metric}"
            value = raw_filters.get(key, "").strip()
            if not value:
                continue
            try:
                filters[key] = float(value)
            except ValueError:
                continue
    return filters


def missing_trade_dates(trade_dates: list[str], existing_dates: set[str]) -> list[str]:
    return [trade_date for trade_date in trade_dates if trade_date not in existing_dates]


def is_unavailable_daily_error(error: Exception) -> bool:
    return "无日线数据" in str(error)


def sync_latest() -> dict:
    started = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        run_id = conn.execute("INSERT INTO sync_runs(started_at,status) VALUES (?,?)", (started, "running")).lastrowid
    try:
        # 日历可能先于日线发布。多取候选日，以确保最后至少能获得 90 个实际可用交易日。
        trade_dates = recent_trade_dates(120)
        with connect() as conn:
            existing_dates = {row[0] for row in conn.execute(
                "SELECT DISTINCT trade_date FROM daily_quotes WHERE trade_date IN (%s)" % ",".join("?" * len(trade_dates)),
                trade_dates,
            )}
        dates_to_fetch = missing_trade_dates(trade_dates, existing_dates)
        quotes = []
        unavailable_dates = set()
        for sync_date in dates_to_fetch:
            try:
                quotes.extend(fetch_quotes(sync_date, include_moneyflow=False, include_basics=False))
            except ProviderError as exc:
                if is_unavailable_daily_error(exc):
                    unavailable_dates.add(sync_date)
                    continue
                raise
        available_dates = [sync_date for sync_date in trade_dates if sync_date not in unavailable_dates]
        if len(available_dates) < 90:
            raise ProviderError(f"仅找到 {len(available_dates)} 个有日线的交易日，无法满足 90 日回补")
        trade_date = available_dates[-1]
        # 最新日额外获取主力资金，覆盖前面的纯历史日线记录。
        quotes.extend(fetch_quotes(trade_date, include_moneyflow=True))
        sectors, sector_error = [], ""
        try:
            sectors = fetch_sectors(trade_date)
        except ProviderError as exc:
            sector_error = str(exc)
        with connect() as conn:
            conn.executemany("""INSERT OR REPLACE INTO daily_quotes (trade_date,ts_code,name,open,high,low,close,pct_chg,vol,amount,turnover_rate,volume_ratio,total_mv,pe,pb,source,main_net_inflow) VALUES (:trade_date,:ts_code,:name,:open,:high,:low,:close,:pct_chg,:vol,:amount,:turnover_rate,:volume_ratio,:total_mv,:pe,:pb,'tushare',:main_net_inflow)""", quotes)
            conn.executemany("""INSERT OR REPLACE INTO sector_snapshots VALUES (:trade_date,:sector_code,:sector_name,:pct_chg,:amount,:main_net_inflow,'eastmoney')""", sectors)
            conn.execute("UPDATE sync_runs SET finished_at=?,trade_date=?,status=?,source=?,message=?,quote_count=?,sector_count=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), trade_date, "partial" if sector_error else "success", "tushare,eastmoney", sector_error, len({row["ts_code"] for row in quotes if row["trade_date"] == trade_date}), len(sectors), run_id))
        calculate_signals(trade_date)
        return {"trade_date": trade_date, "quote_count": len(quotes), "sector_count": len(sectors), "status": "partial" if sector_error else "success", "message": sector_error}
    except Exception as exc:
        with connect() as conn:
            conn.execute("UPDATE sync_runs SET finished_at=?,status=?,message=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), "failed", str(exc), run_id))
        record_system_error("sync_latest", exc)
        raise


def calculate_signals(trade_date: str) -> None:
    with connect() as conn:
        codes = [r[0] for r in conn.execute("SELECT DISTINCT ts_code FROM daily_quotes")]
        for code in codes:
            rows = conn.execute("SELECT * FROM daily_quotes WHERE ts_code=? ORDER BY trade_date DESC LIMIT 40", (code,)).fetchall()[::-1]
            if len(rows) < 26 or rows[-1]["trade_date"] != trade_date:
                continue
            v = calculate([r["close"] for r in rows], [r["high"] for r in rows], [r["low"] for r in rows])
            score, reasons = 0, []
            for key, threshold, label in (("macd", 0, "MACD 金叉区间"), ("kdj_j", 50, "KDJ 偏强"), ("rsi14", 50, "RSI 强势")):
                if v[key] > threshold:
                    score += 25; reasons.append(label)
            if v["nine_turn"] >= 8:
                score += 25; reasons.append("九转上行")
            if rows[-1]["main_net_inflow"] > 0:
                score += 25; reasons.append("主力资金净流入")
            latest = rows[-1]
            conn.execute("""INSERT OR REPLACE INTO stock_signals
                (trade_date,ts_code,name,score,macd,kdj_j,rsi14,boll_position,nine_turn,main_net_inflow,volume_ratio,turnover_rate,amount,total_mv,pe,pb,pct_chg,reasons,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                trade_date, code, latest["name"], score, v["macd"], v["kdj_j"], v["rsi14"],
                v["boll_position"], v["nine_turn"], latest["main_net_inflow"], latest["volume_ratio"],
                latest["turnover_rate"], latest["amount"], latest["total_mv"], latest["pe"], latest["pb"],
                latest["pct_chg"], json.dumps(reasons, ensure_ascii=False), "tushare",
            ))


def dashboard(raw_filters: dict[str, str] | None = None) -> dict:
    initialize()
    filters = normalize_signal_filters(raw_filters or {})
    with connect() as conn:
        run = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        dates = [r[0] for r in conn.execute("SELECT DISTINCT trade_date FROM sector_snapshots ORDER BY trade_date DESC LIMIT 5")][::-1]
        sectors = conn.execute("SELECT * FROM sector_snapshots WHERE trade_date IN (%s) ORDER BY trade_date DESC,pct_chg DESC" % ",".join("?" * len(dates)), dates).fetchall() if dates else []
        signal_date = run["trade_date"] if run and run["trade_date"] else ""
        if signal_date:
            conditions, params = ["trade_date=?"], [signal_date]
            for key, value in filters.items():
                bound, metric = key.split("_", 1)
                conditions.append(f"{metric} {'>=' if bound == 'min' else '<='} ?")
                params.append(value)
            signals = conn.execute(
                "SELECT * FROM stock_signals WHERE " + " AND ".join(conditions) + " ORDER BY score DESC LIMIT 300",
                params,
            ).fetchall()
        else:
            signals = []
    return {
        "run": dict(run) if run else None, "dates": dates, "sectors": [dict(x) for x in sectors],
        "signals": [dict(x) for x in signals], "filters": filters, "system_errors": recent_system_errors(),
    }
