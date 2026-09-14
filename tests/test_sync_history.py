from unittest.mock import patch

from app.providers import recent_trade_dates, trade_dates_since
from app.services import incomplete_snapshot_dates, is_unavailable_daily_error, missing_trade_dates, normalize_sync_start_date, score_signal, sync_latest


def test_first_sync_backfills_all_recent_ninety_open_days():
    trade_dates = [f"2026{i:04d}" for i in range(1, 91)]
    assert missing_trade_dates(trade_dates, set()) == trade_dates


def test_incremental_sync_only_fetches_missing_open_days():
    trade_dates = ["20260729", "20260730", "20260731"]
    assert missing_trade_dates(trade_dates, {"20260729", "20260731"}) == ["20260730"]


def test_incremental_sync_repairs_incomplete_snapshots_but_skips_complete_days():
    dates = ["20260729", "20260730", "20260731"]
    assert incomplete_snapshot_dates(dates, {"20260729": 5_000, "20260730": 12, "20260731": 5_100}, 1_000) == ["20260730"]


def test_sync_skips_open_dates_that_do_not_have_published_daily_quotes_yet():
    assert is_unavailable_daily_error(RuntimeError("20260814 无日线数据（可能尚未收盘或无权限）"))
    assert not is_unavailable_daily_error(RuntimeError("Tushare 网络连接失败"))


def test_recent_trade_dates_requests_only_tushare_open_days():
    class Calendar:
        empty = False
        __getitem__ = lambda self, _: ["20260814", "20260813"]

    class Pro:
        def trade_cal(self, **kwargs):
            assert kwargs["exchange"] == "SSE"
            assert kwargs["is_open"] == "1"
            return Calendar()

    with patch("app.providers._ts", return_value=Pro()):
        assert recent_trade_dates(2) == ["20260813", "20260814"]


def test_trade_dates_since_requests_open_days_from_selected_date():
    class Calendar:
        empty = False
        __getitem__ = lambda self, _: ["20260814", "20260813"]

    class Pro:
        def trade_cal(self, **kwargs):
            assert kwargs["exchange"] == "SSE"
            assert kwargs["is_open"] == "1"
            assert kwargs["start_date"] == "20260801"
            return Calendar()

    with patch("app.providers._ts", return_value=Pro()):
        assert trade_dates_since("20260801") == ["20260813", "20260814"]


def test_normalize_sync_start_date_accepts_browser_date_and_rejects_invalid_values():
    assert normalize_sync_start_date("2026-08-01") == "20260801"
    assert normalize_sync_start_date("") is None
    try:
        normalize_sync_start_date("20260801")
    except ValueError as exc:
        assert str(exc) == "同步起始日期必须为 YYYY-MM-DD"
    else:
        raise AssertionError("invalid date should be rejected")


def test_sync_backfill_requests_real_moneyflow_for_every_historical_date(tmp_path):
    """历史日线回填不能把主力资金流静默写成零。"""
    from app.config import settings
    from app.database import connect, initialize
    from app.providers import SectorFetchResult

    original_data_dir = settings.data_dir
    object.__setattr__(settings, "data_dir", tmp_path)
    dates = ["20260105", "20260106"]
    calls = []

    def fetch_quotes(trade_date, **kwargs):
        calls.append((trade_date, kwargs))
        return []

    try:
        initialize()
        # 日线数量完整、但资金流全为零的旧回填记录仍必须被重拉。
        with connect() as conn:
            conn.executemany(
                "INSERT INTO daily_quotes(trade_date,ts_code,main_net_inflow,source) VALUES (?,?,?,?)",
                [(dates[0], f"{index:06d}.SZ", 0, "old") for index in range(1_000)],
            )
        with patch("app.services.trade_dates_since", return_value=dates), \
             patch("app.services.fetch_quotes", side_effect=fetch_quotes), \
             patch("app.services.fetch_sectors", return_value=SectorFetchResult([], "", [])):
            sync_latest("2026-01-01")
        historical_calls = [kwargs for trade_date, kwargs in calls if trade_date == dates[0]]
        assert historical_calls == [{"include_moneyflow": True, "include_basics": False}]
    finally:
        object.__setattr__(settings, "data_dir", original_data_dir)


def test_nine_turn_eight_and_nine_reduce_score_with_nine_penalised_more():
    metrics = {"macd": 1, "kdj_j": 60, "rsi14": 60}
    score_7, _ = score_signal(metrics, 7, 1)
    score_8, reasons_8 = score_signal(metrics, 8, 1)
    score_9, reasons_9 = score_signal(metrics, 9, 1)

    assert score_7 > score_8 > score_9
    assert "九转 8 高位风险" in reasons_8
    assert "九转 9 高位风险" in reasons_9
