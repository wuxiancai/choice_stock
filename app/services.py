from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import settings
from .database import connect, initialize
from .indicators import calculate
from .providers import MIN_VALID_SECTOR_ROWS, ProviderError, fetch_quotes, fetch_sector_history, fetch_sectors, recent_trade_dates


FILTER_METRICS = (
    "nine_turn", "main_net_inflow", "volume_ratio", "turnover_rate", "amount", "pe", "pb",
)

# A-share daily data contains thousands of listed securities.  A much smaller
# count is a partially written/failed batch and must be picked up by the next
# incremental synchronization rather than treated as complete.
MIN_VALID_DAILY_QUOTE_ROWS = 1_000


def format_cny(value: float | int | None, multiplier: float = 1) -> str:
    """Render a stored monetary value as readable RMB, preserving the data's source unit."""
    if value is None:
        return "—"
    amount = float(value) * multiplier
    sign = "-" if amount < 0 else ""
    absolute = abs(amount)
    if absolute >= 100_000_000:
        rendered, unit = absolute / 100_000_000, "亿"
    else:
        rendered, unit = absolute / 10_000, "万"
    text = f"{rendered:.2f}".rstrip("0").rstrip(".")
    return f"{sign}{text} {unit}"


def format_trade_date(trade_date: str) -> str:
    """Format a YYYYMMDD trade date for display."""
    return f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"


def format_datetime(value: str | None) -> str:
    """Render persisted timestamps in the configured local timezone."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


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


def incomplete_snapshot_dates(
    trade_dates: list[str], counts_by_date: dict[str, int], minimum_rows: int,
) -> list[str]:
    """Return dates without a complete persisted snapshot."""
    return [trade_date for trade_date in trade_dates if counts_by_date.get(trade_date, 0) < minimum_rows]


def is_unavailable_daily_error(error: Exception) -> bool:
    return "无日线数据" in str(error)


def sector_source_summary(selected_source: str, fallback_errors: list[str]) -> str:
    """Make source selection observable without exposing raw URLs or secrets."""
    labels = {
        "tushare_moneyflow": "Tushare 个股资金流行业聚合", "eastmoney": "东方财富", "ths": "同花顺", "tencent": "腾讯",
    }
    attempted = []
    for failure in fallback_errors:
        source, _, detail = failure.partition(":")
        label = labels.get(source, source)
        lowered = detail.lower()
        if "无权限" in detail or "无接口" in detail or "access permission" in lowered:
            detail = "无接口访问权限"
        elif "proxy" in lowered:
            detail = "网络/代理失败"
        elif len(detail.strip()) > 80:
            detail = detail.strip()[:77] + "..."
        attempted.append(f"{label}：失败（{detail.strip()}）")
    selected = labels.get(selected_source, selected_source)
    not_attempted = [label for key, label in labels.items() if key not in {selected_source, *(item.partition(":")[0] for item in fallback_errors)}]
    parts = [f"当日行业数据：{selected} 成功", *attempted]
    if not_attempted:
        parts.append(f"未尝试：{'、'.join(not_attempted)}（前序来源已成功）")
    return "；".join(parts)


def sync_latest() -> dict:
    started = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        run_id = conn.execute("INSERT INTO sync_runs(started_at,status) VALUES (?,?)", (started, "running")).lastrowid
    try:
        # 日历可能先于日线发布。多取候选日，以确保最后至少能获得 90 个实际可用交易日。
        trade_dates = recent_trade_dates(120)
        with connect() as conn:
            quote_counts = dict(conn.execute(
                "SELECT trade_date, COUNT(*) FROM daily_quotes WHERE trade_date IN (%s) GROUP BY trade_date" % ",".join("?" * len(trade_dates)),
                trade_dates,
            ))
        dates_to_fetch = incomplete_snapshot_dates(trade_dates, quote_counts, MIN_VALID_DAILY_QUOTE_ROWS)
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
        sectors, sector_errors, sector_source = [], [], ""
        current_sector_rows = []
        sector_trade_dates = available_dates[-5:]
        with connect() as conn:
            sector_counts = dict(conn.execute(
                "SELECT trade_date, COUNT(*) FROM sector_snapshots WHERE trade_date IN (%s) GROUP BY trade_date" % ",".join("?" * len(sector_trade_dates)),
                    sector_trade_dates,
            ))
        try:
            sector_result = fetch_sectors(trade_date)
            sectors, sector_source = sector_result.rows, sector_result.source
            current_sector_rows = [row for row in sectors if row["trade_date"] == trade_date]
            sector_errors.append(sector_source_summary(sector_source, sector_result.fallback_errors))
            # A fallback that returned a complete data set is successful. Keep the
            # selected source in sync_runs.source, but do not surface failed probes
            # as a user-facing error.
            missing_history_dates = incomplete_snapshot_dates(
                sector_trade_dates[:-1], sector_counts, MIN_VALID_SECTOR_ROWS,
            )
            if missing_history_dates:
                history_rows, history_failures = fetch_sector_history(
                    missing_history_dates, [row["sector_name"] for row in sectors],
                )
                sectors.extend(history_rows)
                sector_errors.extend(history_failures)
        except ProviderError as exc:
            sector_errors.append(str(exc))
            record_system_error("sync_latest.sectors", exc)
        with connect() as conn:
            if current_sector_rows:
                # A new current snapshot replaces any older fallback taxonomy for
                # this date, so five-day matrix rows remain comparable by industry.
                conn.execute("DELETE FROM sector_snapshots WHERE trade_date=?", (trade_date,))
            conn.executemany("""INSERT OR REPLACE INTO daily_quotes (trade_date,ts_code,name,open,high,low,close,pct_chg,vol,amount,turnover_rate,volume_ratio,total_mv,pe,pb,source,main_net_inflow) VALUES (:trade_date,:ts_code,:name,:open,:high,:low,:close,:pct_chg,:vol,:amount,:turnover_rate,:volume_ratio,:total_mv,:pe,:pb,'tushare',:main_net_inflow)""", quotes)
            conn.executemany("""INSERT OR REPLACE INTO sector_snapshots (trade_date,sector_code,sector_name,pct_chg,amount,main_net_inflow,source) VALUES (:trade_date,:sector_code,:sector_name,:pct_chg,:amount,:main_net_inflow,:source)""", sectors)
            completed_sector_dates = {
                row[0] for row in conn.execute(
                    "SELECT trade_date FROM sector_snapshots WHERE trade_date IN (%s) GROUP BY trade_date HAVING COUNT(*) >= ?" % ",".join("?" * len(sector_trade_dates)),
                    [*sector_trade_dates, MIN_VALID_SECTOR_ROWS],
                )
            }
            sector_error = "；".join(sector_errors)
            sector_complete = set(sector_trade_dates) <= completed_sector_dates
            conn.execute("UPDATE sync_runs SET finished_at=?,trade_date=?,status=?,source=?,message=?,quote_count=?,sector_count=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), trade_date, "success" if sector_complete else "partial", f"tushare,{sector_source}" if sector_source else "tushare", sector_error, len({row["ts_code"] for row in quotes if row["trade_date"] == trade_date}), len(sectors), run_id))
        calculate_signals(trade_date)
        return {"trade_date": trade_date, "quote_count": len(quotes), "sector_count": len(sectors), "status": "success" if sector_complete else "partial", "message": sector_error}
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
        signal_date = run["trade_date"] if run and run["trade_date"] else ""
        if signal_date:
            dates = [r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM daily_quotes WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 5",
                (signal_date,),
            )][::-1]
        else:
            dates = []
        if not dates:
            dates = [r[0] for r in conn.execute("SELECT DISTINCT trade_date FROM sector_snapshots ORDER BY trade_date DESC LIMIT 5")][::-1]
        sector_rows = conn.execute("SELECT * FROM sector_snapshots WHERE trade_date IN (%s) ORDER BY trade_date DESC,pct_chg DESC" % ",".join("?" * len(dates)), dates).fetchall() if dates else []
        sector_snapshot_dates = [r[0] for r in conn.execute(
            "SELECT trade_date FROM sector_snapshots WHERE trade_date IN (%s) GROUP BY trade_date HAVING COUNT(*) >= ? ORDER BY trade_date" % ",".join("?" * len(dates)),
            [*dates, MIN_VALID_SECTOR_ROWS],
        )] if dates else []
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
    snapshots_by_sector: dict[str, dict[str, dict]] = {}
    daily_ranks: dict[str, dict[str, int]] = {}
    for trade_date in dates:
        rows_for_day = [dict(row) for row in sector_rows if row["trade_date"] == trade_date]
        daily_ranks[trade_date] = {
            row["sector_name"]: rank for rank, row in enumerate(rows_for_day, start=1)
        }
        for row in rows_for_day:
            snapshots_by_sector.setdefault(row["sector_name"], {})[trade_date] = row

    latest_date = dates[-1] if dates else None
    sectors = []
    for sector_name, snapshots in snapshots_by_sector.items():
        available = list(snapshots.values())
        five_day_inflow = sum((row["main_net_inflow"] or 0) for row in available) / len(dates) if len(available) == len(dates) else None
        daily_changes = [row["pct_chg"] for row in available if row["pct_chg"] is not None]
        five_day_change = sum(daily_changes) / len(dates) if len(daily_changes) == len(dates) else None
        latest = snapshots.get(latest_date) if latest_date else None
        sectors.append({
            "sector_name": sector_name,
            "daily_ranks": {date: daily_ranks[date].get(sector_name) for date in dates},
            "five_day_inflow": five_day_inflow,
            "latest_inflow": latest["main_net_inflow"] if latest else None,
            "five_day_change": five_day_change,
            "latest_change": latest["pct_chg"] if latest else None,
        })
    sectors.sort(key=lambda row: (row["latest_change"] is None, -(row["latest_change"] or 0), row["sector_name"]))
    return {
        "run": dict(run) if run else None, "dates": dates, "sector_dates": dates[::-1], "sector_snapshot_dates": sector_snapshot_dates, "sectors": sectors,
        "signals": [dict(x) for x in signals], "filters": filters, "system_errors": recent_system_errors(),
    }
