from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from functools import lru_cache
from statistics import median
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


def score_signal(metrics: dict[str, float | int | None], nine_turn: int | None, main_net_inflow: float | None) -> tuple[int, list[str]]:
    """Score momentum and funds, explicitly penalising late upward nine-turns."""
    score, reasons = 0, []
    for key, threshold, label in (("macd", 0, "MACD 金叉区间"), ("kdj_j", 50, "KDJ 偏强"), ("rsi14", 50, "RSI 强势")):
        if metrics[key] is not None and metrics[key] > threshold:
            score += 25
            reasons.append(label)
    if main_net_inflow is not None and main_net_inflow > 0:
        score += 25
        reasons.append("主力资金净流入")
    # 上行第 8/9 转是趋势末段警报，9 转的风险更高。分数保持在 0–100。
    if nine_turn == 8:
        score -= 15
        reasons.append("九转 8 高位风险")
    elif nine_turn == 9:
        score -= 30
        reasons.append("九转 9 高位风险")
    return max(score, 0), reasons


RECOMMENDATION_REASONS = "九转启动（1–3）｜涨幅 2%–7%｜主力净流入｜量能不低于近 5 日均量｜RSI<60｜未突破布林上轨"
RECOMMENDATION_MIN_GROUP_SAMPLES = 12
RECOMMENDATION_MIN_SCORE = 60
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


def is_recommended_signal(signal: dict, previous_five_volumes: list[float]) -> bool:
    """Select early upward nine-turn candidates using only data known on the day."""
    if signal.get("nine_turn") not in (1, 2, 3):
        return False
    if not (2 <= (signal.get("pct_chg") or 0) <= 7):
        return False
    if signal.get("main_net_inflow") is None or signal["main_net_inflow"] <= 0:
        return False
    if signal.get("rsi14") is None or signal["rsi14"] >= 60:
        return False
    if signal.get("boll_position") is None or signal["boll_position"] >= 1:
        return False
    current_volume = signal.get("vol")
    return (
        current_volume is not None
        and len(previous_five_volumes) == 5
        and current_volume >= sum(previous_five_volumes) / len(previous_five_volumes)
    )


def _historical_candidate(rows: list[dict], index: int, nine_turn: int | None) -> dict | None:
    if index < 20 or nine_turn not in (1, 2, 3):
        return None
    row = rows[index]
    if not (2 <= (row.get("pct_chg") or 0) <= 7) or (row.get("main_net_inflow") or 0) <= 0:
        return None
    closes = [item["close"] for item in rows[index - 20:index + 1]]
    if any(value is None for value in closes):
        return None
    gains = [max(closes[position] - closes[position - 1], 0) for position in range(7, 21)]
    losses = [max(closes[position - 1] - closes[position], 0) for position in range(7, 21)]
    average_gain, average_loss = sum(gains) / 14, sum(losses) / 14
    rsi14 = 100 if average_loss == 0 else 100 - 100 / (1 + average_gain / average_loss)
    middle = sum(closes[-20:]) / 20
    deviation = (sum((value - middle) ** 2 for value in closes[-20:]) / 20) ** 0.5
    upper, lower = middle + 2 * deviation, middle - 2 * deviation
    return {**row, "nine_turn": nine_turn, "rsi14": rsi14, "boll_position": 0.5 if upper == lower else (closes[-1] - lower) / (upper - lower)}


@lru_cache(maxsize=8)
def historical_recommendation_stats(as_of_date: str) -> dict[tuple[int | None, str | None], dict]:
    """Five-day outcomes of prior matching candidates only; no current-day look-ahead."""
    outcomes: dict[tuple[int | None, str | None], list[float]] = {}
    with connect() as conn:
        cursor = conn.execute(
            "SELECT trade_date,ts_code,industry,close,pct_chg,vol,amount,main_net_inflow "
            "FROM daily_quotes WHERE trade_date<? ORDER BY ts_code,trade_date", (as_of_date,),
        )
        code, rows = None, []

        def collect(stock_rows: list[dict]) -> None:
            upward_run, turns = 0, []
            for index, item in enumerate(stock_rows):
                upward_run = upward_run + 1 if index >= 4 and item["close"] > stock_rows[index - 4]["close"] else 0
                turns.append((upward_run - 1) % 9 + 1 if upward_run else None)
            for index in range(20, len(stock_rows) - 5):
                candidate = _historical_candidate(stock_rows, index, turns[index])
                volumes = [item["vol"] for item in stock_rows[index - 5:index] if item["vol"] is not None]
                if candidate is None or not is_recommended_signal(candidate, volumes):
                    continue
                five_day_return = (stock_rows[index + 5]["close"] / stock_rows[index]["close"] - 1) * 100
                for key in ((candidate["nine_turn"], candidate.get("industry")), (candidate["nine_turn"], None), (None, None)):
                    outcomes.setdefault(key, []).append(five_day_return)

        for row in cursor:
            item = dict(row)
            if code is not None and item["ts_code"] != code:
                collect(rows)
                rows = []
            code = item["ts_code"]
            rows.append(item)
        if rows:
            collect(rows)
    stats = {}
    for key, values in outcomes.items():
        label = "同转同行业" if key[1] is not None else ("同转全部行业" if key[0] is not None else "全部候选")
        stats[key] = {"sample_size": len(values), "win_rate": round(sum(value > 0 for value in values) * 100 / len(values), 2), "median_return": round(float(median(values)), 2), "label": label}
    return stats


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _band_score(value: float | None, bands: tuple[tuple[float, float, float], ...]) -> float:
    if value is None:
        return 0.0
    for lower, upper, score in bands:
        if lower <= value < upper:
            return score
    return 0.0


def _inflow_percentiles(recommendations: list[dict]) -> dict[str, float]:
    inflows = sorted(float(row.get("main_net_inflow") or 0) for row in recommendations)
    if len(inflows) <= 1:
        return {row["ts_code"]: 1.0 for row in recommendations}
    return {
        row["ts_code"]: sum(value <= (row.get("main_net_inflow") or 0) for value in inflows) / len(inflows)
        for row in recommendations
    }


def _recommendation_components(recommendation: dict, profile: dict, inflow_percentile: float) -> dict[str, float]:
    """Score historical edge and each current signal on a transparent 100-point scale."""
    return {
        "历史胜率": _clamp((profile["win_rate"] - 40) * 0.5, 0, 12),
        "历史收益": _clamp((profile["median_return"] + 2) * 1.5, 0, 6),
        "样本": 2 if profile["sample_size"] >= 100 else (1 if profile["sample_size"] >= 30 else 0),
        "资金": 2 + 16 * inflow_percentile,
        "量能": _clamp(3 + ((recommendation.get("volume_vs_5d") or 0) - 1) * 3, 0, 8),
        "MACD": _band_score(recommendation.get("macd"), ((0.2, float("inf"), 10), (0, 0.2, 8), (-0.2, 0, 5), (-1, -0.2, 2))),
        "KDJ": _band_score(recommendation.get("kdj_j"), ((50, 80, 8), (35, 50, 6), (80, 90, 5), (20, 35, 4), (-float("inf"), 20, 1), (90, float("inf"), 1))),
        "RSI": _band_score(recommendation.get("rsi14"), ((45, 58, 8), (35, 45, 6), (58, 60, 5), (30, 35, 4), (-float("inf"), 30, 1))),
        "布林": _band_score(recommendation.get("boll_position"), ((0.4, 0.75, 8), (0.25, 0.4, 6), (0.75, 0.9, 5), (0, 0.25, 3), (-float("inf"), 0, 1), (0.9, float("inf"), 1))),
        "涨幅": _band_score(recommendation.get("pct_chg"), ((3, 5, 8), (2.5, 3, 6), (5, 6, 6), (2, 2.5, 4), (6, 7, 4))),
        "九转": {1: 6, 2: 4, 3: 2}.get(recommendation.get("nine_turn"), 0),
        "技术": _clamp(float(recommendation.get("score") or 0) * 0.06, 0, 6),
    }


def rank_daily_recommendations(recommendations: list[dict], stats: dict[tuple[int | None, str | None], dict]) -> list[dict]:
    fallback = stats.get((None, None), {"sample_size": 0, "win_rate": 0.0, "median_return": 0.0, "label": "样本不足"})
    ranked = []
    inflow_percentiles = _inflow_percentiles(recommendations)
    for recommendation in recommendations:
        profile = stats.get((recommendation["nine_turn"], recommendation.get("industry")))
        if not profile or profile["sample_size"] < RECOMMENDATION_MIN_GROUP_SAMPLES:
            profile = stats.get((recommendation["nine_turn"], None), fallback)
        components = _recommendation_components(recommendation, profile, inflow_percentiles[recommendation["ts_code"]])
        recommendation_score = round(sum(components.values()), 1)
        detail = "｜".join(f"{label}{score:.0f}" for label, score in components.items())
        ranked.append({**recommendation, "historical_win_rate": profile["win_rate"], "historical_median_return": profile["median_return"], "historical_sample_size": profile["sample_size"], "historical_basis": profile["label"], "recommendation_score": recommendation_score, "recommendation_score_detail": detail})
    ranked.sort(key=lambda row: (-row["recommendation_score"], -row["historical_win_rate"], -row["historical_median_return"], -(row["main_net_inflow"] or 0), row["ts_code"]))
    for index, recommendation in enumerate(ranked, start=1):
        recommendation["recommendation_rank"] = index
    return ranked


def select_daily_recommendations(ranked: list[dict]) -> list[dict]:
    """Keep only the strongest, individually scored research candidates."""
    return [row for row in ranked if row["recommendation_score"] >= RECOMMENDATION_MIN_SCORE][:RECOMMENDATION_MAX_CANDIDATES]


def daily_recommendations(conn, signal_date: str) -> list[dict]:
    """Return current early-stage candidates; no future price or later turn is used."""
    rows = conn.execute(
        "SELECT stock_signals.*, daily_quotes.close AS close, daily_quotes.vol AS vol "
        "FROM stock_signals LEFT JOIN daily_quotes "
        "ON daily_quotes.trade_date=stock_signals.trade_date AND daily_quotes.ts_code=stock_signals.ts_code "
        "WHERE stock_signals.trade_date=? AND stock_signals.nine_turn IN (1,2,3)",
        (signal_date,),
    ).fetchall()
    recommendations = []
    for row in rows:
        signal = dict(row)
        volumes = [item[0] for item in conn.execute(
            "SELECT vol FROM daily_quotes WHERE ts_code=? AND trade_date<? ORDER BY trade_date DESC LIMIT 5",
            (signal["ts_code"], signal_date),
        ) if item[0] is not None]
        if is_recommended_signal(signal, volumes):
            signal["volume_vs_5d"] = signal["vol"] / (sum(volumes) / len(volumes))
            signal["recommendation_reasons"] = RECOMMENDATION_REASONS
            signal["tones"] = signal_tones(signal)
            recommendations.append(signal)
    ranked = rank_daily_recommendations(recommendations, historical_recommendation_stats(signal_date))
    return select_daily_recommendations(ranked)


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
        historical_recommendation_stats.cache_clear()
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
            score, reasons = score_signal(v, v["nine_turn"], rows[-1]["main_net_inflow"])
            latest = rows[-1]
            conn.execute("""INSERT OR REPLACE INTO stock_signals
                (trade_date,ts_code,name,industry,score,macd,kdj_j,rsi14,boll_position,nine_turn,bbi,bias,vr,psy,dmi,main_net_inflow,volume_ratio,turnover_rate,amount,total_mv,pe,pb,pct_chg,reasons,source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                trade_date, code, latest["name"], latest["industry"], score, v["macd"], v["kdj_j"], v["rsi14"],
                v["boll_position"], v["nine_turn"], v["bbi"], v["bias"], v["vr"], v["psy"], v["dmi"],
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
            watchlist = conn.execute(
                "SELECT watchlist.ts_code,watchlist.created_at,stock_signals.name,stock_signals.industry,"
                "stock_signals.score,stock_signals.nine_turn,stock_signals.pct_chg,stock_signals.main_net_inflow "
                "FROM watchlist LEFT JOIN stock_signals "
                "ON stock_signals.ts_code=watchlist.ts_code AND stock_signals.trade_date=? "
                "ORDER BY watchlist.created_at DESC",
                (signal_date,),
            ).fetchall()
        else:
            signals = []
            recommendations = []
            watchlist = conn.execute("SELECT ts_code,created_at,NULL AS name,NULL AS industry,NULL AS score,NULL AS nine_turn,NULL AS pct_chg,NULL AS main_net_inflow FROM watchlist ORDER BY created_at DESC").fetchall()
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
