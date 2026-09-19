from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .config import settings
from .database import connect, initialize
from .indicators import calculate
from .providers import MIN_VALID_SECTOR_ROWS, ProviderError, fetch_quotes, fetch_sector_history, fetch_sectors, recent_trade_dates, trade_dates_since


FILTER_METRICS = (
    "nine_turn", "main_net_inflow", "volume_ratio", "turnover_rate", "amount", "pe", "pb",
    "bbi", "bias", "vr", "psy", "dmi",
)

# A-share daily data contains thousands of listed securities.  A much smaller
# count is a partially written/failed batch and must be picked up by the next
# incremental synchronization rather than treated as complete.
MIN_VALID_DAILY_QUOTE_ROWS = 1_000
# 单日全市场资金流完全为零意味着旧回填逻辑或数据源失败，不能视为完整。
MIN_VALID_MONEYFLOW_ROWS = 1_000


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


def format_sector_date(trade_date: str) -> str:
    """Format an industry-board trade date compactly, e.g. 20260814 -> 8.14."""
    return f"{int(trade_date[4:6])}.{trade_date[6:8]}"


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


def watchlist_added_date(value: str | None) -> str:
    """Show the persistent watchlist timestamp as a Shanghai calendar date."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")
    except ValueError:
        return "—"


def _watchlist_performance(conn, ts_code: str, created_at: str, latest_trade_date: str) -> dict:
    """Return holding performance from the first available close on/after adding."""
    added_date = watchlist_added_date(created_at)
    if added_date == "—" or not latest_trade_date:
        return {"added_date": added_date, "holding_return": None}
    start = conn.execute(
        "SELECT close FROM daily_quotes WHERE ts_code=? AND trade_date>=? AND trade_date<=? AND close IS NOT NULL ORDER BY trade_date LIMIT 1",
        (ts_code, added_date.replace("-", ""), latest_trade_date),
    ).fetchone()
    latest = conn.execute(
        "SELECT close FROM daily_quotes WHERE ts_code=? AND trade_date<=? AND close IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
        (ts_code, latest_trade_date),
    ).fetchone()
    holding_return = None
    if start and latest and start["close"]:
        holding_return = round((latest["close"] / start["close"] - 1) * 100, 2)
    return {"added_date": added_date, "holding_return": holding_return}


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


def normalize_signal_filters(raw_filters: dict[str, str]) -> dict[str, float | str]:
    filters: dict[str, float | str] = {}
    stock_code = raw_filters.get("stock_code", "").strip().upper()
    if stock_code:
        filters["stock_code"] = stock_code
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


def signal_tones(signal: dict) -> dict[str, str]:
    """Classify each dashboard metric as favourable, risky, or directionless."""
    def tone(value: float | int | None, positive, risk) -> str:
        if value is None:
            return "neutral"
        if risk(value):
            return "risk"
        if positive(value):
            return "positive"
        return "neutral"

    close, bbi = signal.get("close"), signal.get("bbi")
    return {
        "score": tone(signal.get("score"), lambda value: value >= 50, lambda value: value < 25),
        "nine_turn": tone(signal.get("nine_turn"), lambda value: value <= -8 or 0 < value < 8, lambda value: value >= 8 or -8 < value < 0),
        "pct_chg": tone(signal.get("pct_chg"), lambda value: 0 <= value < 7, lambda value: value < 0 or value >= 7),
        "main_net_inflow": tone(signal.get("main_net_inflow"), lambda value: value > 0, lambda value: value < 0),
        "pe": tone(signal.get("pe"), lambda value: 0 < value < 20, lambda value: value <= 0 or value >= 50),
        "volume_ratio": tone(signal.get("volume_ratio"), lambda value: 1 <= value <= 3, lambda value: value < 0.8 or value > 5),
        "turnover_rate": tone(signal.get("turnover_rate"), lambda value: 3 <= value <= 15, lambda value: value < 1 or value > 20),
        "macd": tone(signal.get("macd"), lambda value: value > 0, lambda value: value < 0),
        "kdj_j": tone(signal.get("kdj_j"), lambda value: value < 80, lambda value: value > 100),
        "rsi14": tone(signal.get("rsi14"), lambda value: value < 30, lambda value: value > 70),
        "boll_position": tone(signal.get("boll_position"), lambda value: value <= 0, lambda value: value >= 1),
        "bbi": "neutral" if close is None or bbi is None else ("positive" if close >= bbi else "risk"),
        "bias": tone(signal.get("bias"), lambda value: value <= -6, lambda value: value >= 6),
        "vr": tone(signal.get("vr"), lambda value: value < 70, lambda value: value > 450),
        "psy": tone(signal.get("psy"), lambda value: value < 25, lambda value: value > 75),
        # 当前仅保存 ADX 强度，未保存 +/-DI 方向，不能据此判断多空。
        "dmi": "neutral",
        # 成交额与市值本身没有多空方向，保持中性而不伪造交易信号。
        "amount": "neutral",
        "total_mv": "neutral",
        "pb": tone(signal.get("pb"), lambda value: 0 < value <= 1, lambda value: value <= 0 or value >= 5),
    }


def missing_trade_dates(trade_dates: list[str], existing_dates: set[str]) -> list[str]:
    return [trade_date for trade_date in trade_dates if trade_date not in existing_dates]


def incomplete_snapshot_dates(
    trade_dates: list[str], counts_by_date: dict[str, int], minimum_rows: int,
) -> list[str]:
    """Return dates without a complete persisted snapshot."""
    return [trade_date for trade_date in trade_dates if counts_by_date.get(trade_date, 0) < minimum_rows]


def is_unavailable_daily_error(error: Exception) -> bool:
    return "无日线数据" in str(error)


NINE_TURN_SCORES = {1: 5, 2: 12, 3: 25, 4: 23, 5: 20, 6: 14, 7: 7, 8: 0, 9: 0}


def _nine_turn_score(nine_turn: int | None) -> float:
    """Score the product's upward 3–5 entry window; 8/9 are exhaustion risks."""
    return float(NINE_TURN_SCORES.get(nine_turn, 0))


def _fund_flow_score(main_net_inflow: float | None, amount: float | None) -> float:
    """Use net inflow as a share of turnover, avoiding a large-cap absolute-value bias."""
    if main_net_inflow is None or main_net_inflow <= 0:
        return 0.0
    if amount is None or amount <= 0:  # Tushare amount is thousands of RMB.
        return 6.0
    ratio = main_net_inflow / (amount * 1000)
    return _band_score(ratio, ((0.015, float("inf"), 20), (0.008, 0.015, 17), (0.003, 0.008, 14), (0.001, 0.003, 10), (0, 0.001, 6)))


def _band_score(value: float | None, bands: tuple[tuple[float, float, float], ...]) -> float:
    if value is None:
        return 0.0
    for lower, upper, score in bands:
        if lower <= value < upper:
            return score
    return 0.0


def score_signal(metrics: dict[str, float | int | None], nine_turn: int | None, main_net_inflow: float | None, quote: dict | None = None) -> tuple[int, list[str]]:
    """Score four resonance dimensions plus independent timing and funding dimensions."""
    quote = quote or {}
    ma5, ma20 = metrics.get("ma5"), metrics.get("ma20")
    close = quote.get("close")
    trend = 30 if None not in (ma5, ma20, close) and ma5 > ma20 and close >= ma20 else 0
    dif, dea, histogram = metrics.get("macd_dif", metrics.get("macd")), metrics.get("macd_dea"), metrics.get("macd_histogram")
    momentum = 20 if None not in (dif, dea, histogram) and dif > dea and histogram > 0 and dif >= 0 else 0
    rsi = metrics.get("rsi14")
    position = 15 if rsi is not None and 50 <= rsi < 70 else 0
    volume_ratio, turnover_rate = quote.get("volume_ratio"), quote.get("turnover_rate")
    volume = 15 if volume_ratio is not None and volume_ratio >= 1.2 and turnover_rate is not None and turnover_rate >= 3 else 0
    timing = {3: 10, 4: 8, 5: 6}.get(nine_turn, 0)
    funding = _fund_flow_score(main_net_inflow, quote.get("amount")) / 2
    components = {
        "均线趋势": trend,
        "MACD动能": momentum,
        "RSI位置": position,
        "成交量确认": volume,
        "九转时点": timing,
        "资金强度": funding,
    }
    reasons = [label for label, value in components.items() if value > 0]
    return round(sum(components.values())), reasons


RECOMMENDATION_REASONS = "日线四重共振已确认：均线金叉｜MACD红柱放大｜RSI低位回升｜放量及后续3日确认｜九转时点与主力资金确认"
RECOMMENDATION_MIN_SCORE = 80
RECOMMENDATION_MAX_CANDIDATES = 30
TS_CODE_PATTERN = re.compile(r"^\d{6}\.(?:SZ|SH|BJ)$")


def _watchlist_ts_code(value: str) -> str:
    ts_code = value.strip().upper()
    if not TS_CODE_PATTERN.fullmatch(ts_code):
        raise ValueError("股票代码必须是完整的 Tushare 代码，例如 000001.SZ")
    return ts_code


def add_to_watchlist(value: str) -> bool:
    """Persist a stock once; repeat clicks are intentionally idempotent."""
    ts_code = _watchlist_ts_code(value)
    with connect() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO watchlist(ts_code,created_at) VALUES (?,?)",
            (ts_code, datetime.now(timezone.utc).isoformat()),
        )
    return cursor.rowcount == 1


def remove_from_watchlist(value: str) -> bool:
    ts_code = _watchlist_ts_code(value)
    with connect() as conn:
        cursor = conn.execute("DELETE FROM watchlist WHERE ts_code=?", (ts_code,))
    return cursor.rowcount == 1


def _metrics(rows: list[dict]) -> dict | None:
    if len(rows) < 26 or any(row["close"] is None or row["high"] is None or row["low"] is None or row["vol"] is None for row in rows):
        return None
    return calculate([row["close"] for row in rows], [row["high"] for row in rows], [row["low"] for row in rows], [row["vol"] for row in rows])


def _resonance_candidate(rows: list[dict]) -> dict | None:
    """Confirm a setup three trading days after its MA golden-cross day."""
    cross_index = len(rows) - 4
    if cross_index < 26:
        return None
    cross_metrics = _metrics(rows[:cross_index + 1])
    previous_metrics = _metrics(rows[:cross_index])
    current_metrics = _metrics(rows)
    if not cross_metrics or not previous_metrics or not current_metrics:
        return None
    if not (previous_metrics["ma5"] <= previous_metrics["ma20"] < cross_metrics["ma5"] and cross_metrics["ma5"] > previous_metrics["ma5"] and cross_metrics["ma20"] > previous_metrics["ma20"] and rows[cross_index]["close"] >= cross_metrics["ma20"]):
        return None
    if not (cross_metrics["macd_dif"] > cross_metrics["macd_dea"] and cross_metrics["macd_histogram"] > 0 and cross_metrics["macd_histogram"] > previous_metrics["macd_histogram"] and (cross_metrics["macd_dif"] >= 0 or previous_metrics["macd_dif"] <= 0 < cross_metrics["macd_dif"])):
        return None
    rsi_history = [_metrics(rows[:index + 1])["rsi14"] for index in range(max(25, cross_index - 20), len(rows)) if _metrics(rows[:index + 1])]
    if not (50 <= current_metrics["rsi14"] < 70 and any(value < 30 for value in rsi_history)):
        return None
    for index in range(cross_index, len(rows)):
        previous_volumes = [row["vol"] for row in rows[index - 5:index]]
        multiplier = 1.2 if index == cross_index else 1
        if len(previous_volumes) != 5 or rows[index]["vol"] < multiplier * sum(previous_volumes) / 5:
            return None
    obv_rising = len(rows) >= 36 and current_metrics["obv"] > _metrics(rows[:-5])["obv"] > _metrics(rows[:-10])["obv"]
    turnover, volume_ratio = rows[-1].get("turnover_rate"), rows[-1].get("volume_ratio")
    flow_note = "换手健康（3%–8%）" if turnover is not None and 3 <= turnover <= 8 else "换手偏离健康区间"
    if turnover is not None and volume_ratio is not None and 2 <= volume_ratio <= 3 and turnover > 5:
        flow_note = "量比2–3且换手>5%，主力温和介入"
    elif turnover is not None and volume_ratio is not None and volume_ratio > 5 and turnover > 10:
        flow_note = "量比>5且换手>10%，警惕高位出货"
    volume_multiple = rows[cross_index]["vol"] / (sum(row["vol"] for row in rows[cross_index - 5:cross_index]) / 5)
    nine_turn = current_metrics["nine_turn"]
    if nine_turn not in (3, 4, 5) or rows[-1]["main_net_inflow"] is None or rows[-1]["main_net_inflow"] <= 0:
        return None
    components = {
        "均线趋势": 30 if (cross_metrics["ma5"] - cross_metrics["ma20"]) / cross_metrics["ma20"] >= .01 else 26,
        "MACD动能": 20 if cross_metrics["macd_histogram"] >= previous_metrics["macd_histogram"] * 1.2 else 16,
        "RSI位置": 15 if current_metrics["rsi14"] < 60 else 12,
        "成交量确认": 15 if volume_multiple >= 1.5 else 12,
        "九转时点": {3: 10, 4: 8, 5: 6}[nine_turn],
        "资金强度": _fund_flow_score(rows[-1]["main_net_inflow"], rows[-1]["amount"]) / 2,
    }
    latest = {**rows[-1], **current_metrics, "nine_turn": nine_turn, "golden_cross_date": rows[cross_index]["trade_date"], "volume_vs_5d": volume_multiple, "obv_status": "OBV持续抬高，资金长期留守" if obv_rising else "OBV未持续抬高，资金确认不足", "flow_note": flow_note}
    latest["recommendation_score"] = round(sum(components.values()), 1)
    latest["score"] = latest["recommendation_score"]
    latest["recommendation_score_detail"] = "｜".join(f"{label}{score:.0f}" for label, score in components.items())
    latest["recommendation_reasons"] = RECOMMENDATION_REASONS
    return latest


def daily_recommendations(conn, signal_date: str) -> list[dict]:
    """Return only setups whose post-cross three trading-day volume confirmation exists."""
    recommendations = []
    # One bounded query prevents a 5,000+ stock N+1 scan every time the page loads.
    quote_rows = conn.execute(
        "SELECT * FROM daily_quotes WHERE trade_date IN "
        "(SELECT DISTINCT trade_date FROM daily_quotes WHERE trade_date<=? ORDER BY trade_date DESC LIMIT 50) "
        "ORDER BY ts_code,trade_date",
        (signal_date,),
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for row in quote_rows:
        grouped.setdefault(row["ts_code"], []).append(dict(row))
    for rows in grouped.values():
        candidate = _resonance_candidate(rows)
        if candidate and candidate["recommendation_score"] >= RECOMMENDATION_MIN_SCORE:
            candidate["tones"] = signal_tones(candidate)
            recommendations.append(candidate)
    recommendations.sort(key=lambda row: (-row["recommendation_score"], row["ts_code"]))
    for index, recommendation in enumerate(recommendations, start=1):
        recommendation["recommendation_rank"] = index
    return recommendations[:RECOMMENDATION_MAX_CANDIDATES]


def normalize_sync_start_date(value: str | None) -> str | None:
    """Validate a browser date input and convert it to Tushare's YYYYMMDD form."""
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("同步起始日期必须为 YYYY-MM-DD") from exc
    if parsed > date.today():
        raise ValueError("同步起始日期不能晚于今天")
    return parsed.strftime("%Y%m%d")


def sector_source_summary(selected_source: str, fallback_errors: list[str]) -> str:
    """Make source selection observable without exposing raw URLs or secrets."""
    labels = {
        "tushare_moneyflow": "Tushare 申万一级行业聚合", "tencent": "腾讯申万一级行业聚合",
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


def sync_latest(start_date: str | None = None) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        run_id = conn.execute("INSERT INTO sync_runs(started_at,status) VALUES (?,?)", (started, "running")).lastrowid
    try:
        normalized_start_date = normalize_sync_start_date(start_date)
        # 未指定日期时沿用首次同步的 90 个有效交易日回补策略；指定后严格从该日期起同步。
        trade_dates = trade_dates_since(normalized_start_date) if normalized_start_date else recent_trade_dates(120)
        with connect() as conn:
            quote_counts = dict(conn.execute(
                "SELECT trade_date, COUNT(*) FROM daily_quotes WHERE trade_date IN (%s) GROUP BY trade_date" % ",".join("?" * len(trade_dates)),
                trade_dates,
            ))
            moneyflow_counts = dict(conn.execute(
                "SELECT trade_date, SUM(CASE WHEN ABS(COALESCE(main_net_inflow, 0)) > 0.01 THEN 1 ELSE 0 END) "
                "FROM daily_quotes WHERE trade_date IN (%s) GROUP BY trade_date" % ",".join("?" * len(trade_dates)),
                trade_dates,
            ))
        dates_to_fetch = sorted(set(
            incomplete_snapshot_dates(trade_dates, quote_counts, MIN_VALID_DAILY_QUOTE_ROWS)
            + incomplete_snapshot_dates(trade_dates, moneyflow_counts, MIN_VALID_MONEYFLOW_ROWS)
        ))
        quotes = []
        unavailable_dates = set()
        for sync_date in dates_to_fetch:
            try:
                quotes.extend(fetch_quotes(sync_date, include_moneyflow=True, include_basics=False))
            except ProviderError as exc:
                if is_unavailable_daily_error(exc):
                    unavailable_dates.add(sync_date)
                    continue
                raise
        available_dates = [sync_date for sync_date in trade_dates if sync_date not in unavailable_dates]
        minimum_available_dates = 1 if normalized_start_date else 90
        if len(available_dates) < minimum_available_dates:
            if normalized_start_date:
                raise ProviderError(f"{normalized_start_date} 起没有已发布日线数据")
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
            conn.executemany("""INSERT OR REPLACE INTO daily_quotes (trade_date,ts_code,name,open,high,low,close,pct_chg,vol,amount,industry,turnover_rate,volume_ratio,total_mv,pe,pb,source,main_net_inflow) VALUES (:trade_date,:ts_code,:name,:open,:high,:low,:close,:pct_chg,:vol,:amount,:industry,:turnover_rate,:volume_ratio,:total_mv,:pe,:pb,'tushare',:main_net_inflow)""", quotes)
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
            v = calculate(
                [r["close"] for r in rows], [r["high"] for r in rows], [r["low"] for r in rows],
                [r["vol"] for r in rows],
            )
            latest = rows[-1]
            score, reasons = score_signal(v, v["nine_turn"], latest["main_net_inflow"], dict(latest))
            conn.execute("""INSERT OR REPLACE INTO stock_signals
                (trade_date,ts_code,name,industry,score,macd,kdj_j,rsi14,boll_position,nine_turn,ma5,ma20,macd_dea,macd_histogram,obv,bbi,bias,vr,psy,dmi,main_net_inflow,volume_ratio,turnover_rate,amount,total_mv,pe,pb,pct_chg,reasons,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                trade_date, code, latest["name"], latest["industry"], score, v["macd"], v["kdj_j"], v["rsi14"],
                v["boll_position"], v["nine_turn"], v["ma5"], v["ma20"], v["macd_dea"], v["macd_histogram"], v["obv"], v["bbi"], v["bias"], v["vr"], v["psy"], v["dmi"],
                latest["main_net_inflow"], latest["volume_ratio"],
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
            conditions, params = ["stock_signals.trade_date=?"], [signal_date]
            for key, value in filters.items():
                if key == "stock_code":
                    conditions.append("(stock_signals.ts_code LIKE ? OR stock_signals.name LIKE ?)")
                    params.extend((f"{value}%", f"{value}%"))
                    continue
                bound, metric = key.split("_", 1)
                conditions.append(f"stock_signals.{metric} {'>=' if bound == 'min' else '<='} ?")
                params.append(value)
            signals = conn.execute(
                "SELECT stock_signals.*, daily_quotes.close AS close FROM stock_signals "
                "LEFT JOIN daily_quotes ON daily_quotes.trade_date=stock_signals.trade_date AND daily_quotes.ts_code=stock_signals.ts_code "
                "WHERE " + " AND ".join(conditions) + " ORDER BY stock_signals.main_net_inflow IS NULL, stock_signals.main_net_inflow DESC, stock_signals.score DESC LIMIT 300",
                params,
            ).fetchall()
            recommendations = daily_recommendations(conn, signal_date)
            watchlist_rows = conn.execute(
                "SELECT watchlist.ts_code,watchlist.created_at,stock_signals.name,stock_signals.industry,"
                "stock_signals.score,stock_signals.nine_turn,stock_signals.pct_chg,stock_signals.main_net_inflow "
                "FROM watchlist LEFT JOIN stock_signals "
                "ON stock_signals.ts_code=watchlist.ts_code AND stock_signals.trade_date=? "
                "ORDER BY watchlist.created_at DESC",
                (signal_date,),
            ).fetchall()
            watchlist = [{**dict(row), **_watchlist_performance(conn, row["ts_code"], row["created_at"], signal_date)} for row in watchlist_rows]
        else:
            signals = []
            recommendations = []
            watchlist = [
                {**dict(row), "added_date": watchlist_added_date(row["created_at"]), "holding_return": None}
                for row in conn.execute("SELECT ts_code,created_at,NULL AS name,NULL AS industry,NULL AS score,NULL AS nine_turn,NULL AS pct_chg,NULL AS main_net_inflow FROM watchlist ORDER BY created_at DESC").fetchall()
            ]
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
        "signals": [{**dict(row), "tones": signal_tones(dict(row))} for row in signals], "recommendations": recommendations,
        "watchlist": [{**dict(row), "tones": signal_tones(dict(row))} for row in watchlist],
        "filters": filters, "system_errors": recent_system_errors(),
    }
