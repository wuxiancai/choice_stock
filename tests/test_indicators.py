from app.indicators import calculate
from app.providers import ProviderError, SectorFetchResult, fetch_sectors
from app.services import dashboard, format_cny, format_trade_date, normalize_signal_filters, recent_system_errors, record_system_error, sync_latest
from app.config import settings
from app.database import connect, initialize
from jinja2 import Environment, FileSystemLoader
from unittest.mock import patch
import os


def test_calculate_returns_all_requested_technical_metrics():
    closes = [10 + i * 0.1 for i in range(35)]
    metrics = calculate(closes, [x + 0.2 for x in closes], [x - 0.2 for x in closes])
    assert {"macd", "kdj_j", "rsi14", "boll_position", "nine_turn"} <= metrics.keys()
    assert metrics["macd"] > 0
    assert 0 <= metrics["rsi14"] <= 100


def test_calculate_marks_a_completed_downward_nine_turn_as_negative():
    closes = [100 - i for i in range(35)]
    metrics = calculate(closes, [x + 0.2 for x in closes], [x - 0.2 for x in closes])
    assert metrics["nine_turn"] == -9


def test_format_cny_uses_yi_or_wan_with_source_unit_multiplier():
    assert format_cny(1_000_000_000) == "10 亿"
    assert format_cny(9_999_0000) == "9999 万"
    assert format_cny(-50_000_000) == "-5000 万"
    assert format_cny(10_000, multiplier=1000) == "1000 万"
    assert format_cny(None) == "—"


def test_signal_filters_accept_supported_metrics_and_ignore_removed_metrics():
    filters = normalize_signal_filters({"min_volume_ratio": "1.2", "max_pb": "5", "min_macd": "0", "min_pct_chg": "2"})
    assert filters == {"min_volume_ratio": 1.2, "max_pb": 5.0}


def test_dashboard_template_renders_historical_signal_with_new_nullable_fields():
    environment = Environment(loader=FileSystemLoader("app/templates"))
    environment.filters["cny"] = format_cny
    environment.filters["trade_date"] = format_trade_date
    template = environment.get_template("index.html")
    signal = {
        "name": "测试", "ts_code": "000001.SZ", "score": 0, "macd": 0, "kdj_j": 0,
        "rsi14": 0, "boll_position": 0, "nine_turn": 4, "volume_ratio": None,
        "turnover_rate": None, "amount": 100, "total_mv": None, "pe": None, "pb": None,
        "pct_chg": None, "main_net_inflow": None, "reasons": "[]",
    }
    html = template.render(dashboard={"run": None, "dates": [], "sector_dates": [], "sectors": [], "signals": [signal], "filters": {}, "system_errors": []})
    assert "000001.SZ" in html
    assert "—" in html
    assert ">4</td>" in html
    assert 'id="signal-table"' in html
    assert 'data-sort-type="number"' in html
    filter_section = html.split('<div class="card"><h2>当日技术信号</h2>', 1)[0]
    for field in ("macd", "kdj_j", "rsi14", "boll_position", "pct_chg"):
        assert f'name="min_{field}"' not in filter_section


def test_dashboard_adds_daily_sector_ranks(tmp_path):
    original_data_dir = settings.data_dir
    object.__setattr__(settings, "data_dir", tmp_path)
    try:
        initialize()
        with connect() as conn:
            conn.executemany(
                "INSERT INTO sector_snapshots VALUES (?,?,?,?,?,?,?)",
                [
                    ("20260813", "a", "行业A", 1.2, None, 100_000_000, "test"),
                    ("20260813", "b", "行业B", 0.5, None, -50_000_000, "test"),
                    ("20260812", "c", "行业C", 2.0, None, 0, "test"),
                    ("20260812", "d", "行业D", 1.0, None, 0, "test"),
                ],
            )
        result = dashboard()
        assert result["sector_dates"] == ["20260813", "20260812"]
        assert [(row["sector_name"], row["daily_ranks"], row["five_day_inflow"], row["latest_inflow"]) for row in result["sectors"]] == [
            ("行业A", {"20260812": None, "20260813": 1}, None, 100_000_000),
            ("行业B", {"20260812": None, "20260813": 2}, None, -50_000_000),
            ("行业C", {"20260812": 1, "20260813": None}, None, None),
            ("行业D", {"20260812": 2, "20260813": None}, None, None),
        ]
        assert result["sectors"][0]["latest_change"] == 1.2
    finally:
        object.__setattr__(settings, "data_dir", original_data_dir)


def test_dashboard_keeps_five_trade_date_headers_when_sector_history_is_incomplete(tmp_path):
    original_data_dir = settings.data_dir
    object.__setattr__(settings, "data_dir", tmp_path)
    dates = ["20260807", "20260810", "20260811", "20260812", "20260813"]
    try:
        initialize()
        with connect() as conn:
            conn.executemany(
                "INSERT INTO daily_quotes(trade_date,ts_code,source) VALUES (?,?,?)",
                [(trade_date, "000001.SZ", "test") for trade_date in dates],
            )
            conn.execute("INSERT INTO sync_runs(started_at,trade_date,status) VALUES (?,?,?)", ("now", dates[-1], "partial"))
            conn.execute("INSERT INTO sector_snapshots VALUES (?,?,?,?,?,?,?)", (dates[-1], "a", "行业A", 1, None, 1, "test"))
        result = dashboard()
        assert result["sector_dates"] == dates[::-1]
        assert result["sector_snapshot_dates"] == [dates[-1]]
        assert result["sectors"][0]["daily_ranks"] == {date: (1 if date == dates[-1] else None) for date in dates}
    finally:
        object.__setattr__(settings, "data_dir", original_data_dir)


def test_system_errors_are_persisted_and_tushare_token_is_redacted(tmp_path):
    original_data_dir = settings.data_dir
    original_token = settings.tushare_token
    object.__setattr__(settings, "data_dir", tmp_path)
    object.__setattr__(settings, "tushare_token", "secret-token")
    try:
        initialize()
        record_system_error("test", RuntimeError("provider failed: secret-token"))
        logs = recent_system_errors()
        assert logs[0]["level"] == "ERROR"
        assert logs[0]["source"] == "test"
        assert logs[0]["message"] == "provider failed: [REDACTED]"
    finally:
        object.__setattr__(settings, "data_dir", original_data_dir)
        object.__setattr__(settings, "tushare_token", original_token)


def test_partial_sector_sync_persists_provider_error(tmp_path):
    original_data_dir = settings.data_dir
    object.__setattr__(settings, "data_dir", tmp_path)
    dates = [f"202601{i:02d}" for i in range(1, 91)]
    try:
        initialize()
        with patch("app.services.recent_trade_dates", return_value=dates), \
             patch("app.services.fetch_quotes", return_value=[]), \
             patch("app.services.fetch_sectors", side_effect=ProviderError("东方财富行业资金流拉取失败：代理不可用")):
            result = sync_latest()
        assert result["status"] == "partial"
        logs = recent_system_errors()
        assert logs[0]["source"] == "sync_latest.sectors"
        assert "代理不可用" in logs[0]["message"]
    finally:
        object.__setattr__(settings, "data_dir", original_data_dir)


def test_sync_backfills_the_previous_four_sector_trade_dates(tmp_path):
    original_data_dir = settings.data_dir
    object.__setattr__(settings, "data_dir", tmp_path)
    dates = [f"202601{i:02d}" for i in range(1, 91)]
    latest_date, history_dates = dates[-1], dates[-5:-1]
    latest_rows = [
        {"trade_date": latest_date, "sector_code": f"s{index}", "sector_name": f"行业{index}", "pct_chg": 1.0, "amount": 100, "main_net_inflow": 100, "source": "eastmoney"}
        for index in range(30)
    ]
    history_rows = [
        {"trade_date": trade_date, "sector_code": f"s{index}", "sector_name": f"行业{index}", "pct_chg": 1.0, "amount": 100, "main_net_inflow": 100, "source": "eastmoney_history"}
        for trade_date in history_dates for index in range(30)
    ]
    try:
        initialize()
        with patch("app.services.recent_trade_dates", return_value=dates), \
             patch("app.services.fetch_quotes", return_value=[]), \
             patch("app.services.fetch_sectors", return_value=SectorFetchResult(latest_rows, "ths", ["tushare_ths: 无权限", "eastmoney: 超时"])), \
             patch("app.services.fetch_sector_history", return_value=(history_rows, [])) as history_fetch:
            result = sync_latest()
        assert result["status"] == "success"
        assert result["message"] == ""
        history_fetch.assert_called_once_with(history_dates, [f"行业{index}" for index in range(30)])
        with connect() as conn:
            persisted_dates = [row[0] for row in conn.execute("SELECT DISTINCT trade_date FROM sector_snapshots ORDER BY trade_date")]
        assert persisted_dates == dates[-5:]
    finally:
        object.__setattr__(settings, "data_dir", original_data_dir)


def test_sector_fetch_retries_once_without_proxy_after_proxy_error():
    snapshots = []

    class EmptyFrame:
        def to_dict(self, orient):
            return [
                {"行业": f"测试行业{index}", "今日涨跌幅": 1.2, "今日主力净流入_净额": 100}
                for index in range(30)
            ]

    def fetch_frame():
        snapshots.append({key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")})
        if len(snapshots) == 1:
            raise RuntimeError("ProxyError: connection reset")
        return EmptyFrame()

    with patch.dict(os.environ, {"HTTP_PROXY": "http://proxy", "HTTPS_PROXY": "http://proxy", "ALL_PROXY": "http://proxy"}, clear=False), \
         patch("app.providers._fetch_tushare_ths_sector_frame", side_effect=ProviderError("无权限")), \
         patch("app.providers._fetch_tushare_dc_sector_frame", side_effect=ProviderError("无权限")), \
         patch("app.providers._fetch_sector_frame", side_effect=fetch_frame):
        result = fetch_sectors("20260101")
        assert result.source == "eastmoney"
        assert len(result.rows) == 30
        assert snapshots[0]["HTTP_PROXY"] == "http://proxy"
        assert snapshots[1] == {"HTTP_PROXY": None, "HTTPS_PROXY": None, "ALL_PROXY": None}
        assert os.environ["HTTP_PROXY"] == "http://proxy"


def test_sector_fetch_preserves_proxy_and_direct_failures():
    with patch("app.providers._fetch_tushare_ths_sector_frame", side_effect=ProviderError("无权限")), \
         patch("app.providers._fetch_tushare_dc_sector_frame", side_effect=ProviderError("无权限")), \
         patch("app.providers._fetch_sector_frame", side_effect=[RuntimeError("ProxyError: proxy down"), RuntimeError("direct DNS failure")]), \
         patch("app.providers._fetch_ths_sector_frame", side_effect=RuntimeError("ths unavailable")):
        try:
            fetch_sectors("20260101")
        except ProviderError as exc:
            assert "ProxyError: proxy down" in str(exc)
            assert "direct DNS failure" in str(exc)
            assert "ths unavailable" in str(exc)
        else:
            raise AssertionError("Expected the failed proxy retry to raise ProviderError")


def test_sector_fetch_uses_independent_ths_backup_when_higher_priority_sources_fail():
    class ThsFrame:
        def to_dict(self, orient):
            assert orient == "records"
            return [
                {"行业": f"半导体{index}", "行业-涨跌幅": "2.50%", "净额": "1.25"}
                for index in range(30)
            ]

    with patch("app.providers._fetch_tushare_ths_sector_frame", side_effect=ProviderError("无权限")), \
         patch("app.providers._fetch_tushare_dc_sector_frame", side_effect=ProviderError("无权限")), \
         patch("app.providers._fetch_eastmoney_sector_frame_with_retry", side_effect=RuntimeError("空响应")), \
         patch("app.providers._fetch_ths_sector_frame", return_value=ThsFrame()):
        result = fetch_sectors("20260101")

    assert isinstance(result, SectorFetchResult)
    assert result.source == "ths"
    assert result.rows[0] == {
        "trade_date": "20260101", "sector_code": "半导体0", "sector_name": "半导体0",
        "pct_chg": 2.5, "amount": 125000000.0, "main_net_inflow": 125000000.0, "source": "ths",
    }
    assert len(result.rows) == 30
    assert len(result.fallback_errors) == 3
