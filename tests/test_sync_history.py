from app.services import is_unavailable_daily_error, missing_trade_dates


def test_first_sync_backfills_all_recent_ninety_open_days():
    trade_dates = [f"2026{i:04d}" for i in range(1, 91)]
    assert missing_trade_dates(trade_dates, set()) == trade_dates


def test_incremental_sync_only_fetches_missing_open_days():
    trade_dates = ["20260729", "20260730", "20260731"]
    assert missing_trade_dates(trade_dates, {"20260729", "20260731"}) == ["20260730"]


def test_sync_skips_open_dates_that_do_not_have_published_daily_quotes_yet():
    assert is_unavailable_daily_error(RuntimeError("20260814 无日线数据（可能尚未收盘或无权限）"))
    assert not is_unavailable_daily_error(RuntimeError("Tushare 网络连接失败"))
