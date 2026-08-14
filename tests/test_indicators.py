from app.indicators import calculate
from app.providers import ProviderError, fetch_sectors
from app.services import normalize_signal_filters, recent_system_errors, record_system_error, sync_latest
from app.config import settings
from app.database import initialize
from jinja2 import Environment, FileSystemLoader
from unittest.mock import patch
import os


def test_calculate_returns_all_requested_technical_metrics():
    closes = [10 + i * 0.1 for i in range(35)]
    metrics = calculate(closes, [x + 0.2 for x in closes], [x - 0.2 for x in closes])
    assert {"macd", "kdj_j", "rsi14", "boll_position", "nine_turn"} <= metrics.keys()
    assert metrics["macd"] > 0
    assert 0 <= metrics["rsi14"] <= 100


def test_signal_filters_accept_supported_metrics_and_ignore_removed_metrics():
    filters = normalize_signal_filters({"min_volume_ratio": "1.2", "max_pb": "5", "min_macd": "0", "min_pct_chg": "2"})
    assert filters == {"min_volume_ratio": 1.2, "max_pb": 5.0}


def test_dashboard_template_renders_historical_signal_with_new_nullable_fields():
    template = Environment(loader=FileSystemLoader("app/templates")).get_template("index.html")
    signal = {
        "name": "测试", "ts_code": "000001.SZ", "score": 0, "macd": 0, "kdj_j": 0,
        "rsi14": 0, "boll_position": 0, "nine_turn": 0, "volume_ratio": None,
        "turnover_rate": None, "amount": 100, "total_mv": None, "pe": None, "pb": None,
        "pct_chg": None, "main_net_inflow": None, "reasons": "[]",
    }
    html = template.render(dashboard={"run": None, "dates": [], "sectors": [], "signals": [signal], "filters": {}})
    assert "000001.SZ" in html
    assert "—" in html
    assert 'id="signal-table"' in html
    assert 'data-sort-type="number"' in html
    filter_section = html.split('<div class="card"><h2>当日技术信号</h2>', 1)[0]
    for field in ("macd", "kdj_j", "rsi14", "boll_position", "pct_chg"):
        assert f'name="min_{field}"' not in filter_section


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


def test_sector_fetch_retries_once_without_proxy_after_proxy_error():
    snapshots = []

    class EmptyFrame:
        def itertuples(self):
            return []

    def fetch_frame():
        snapshots.append({key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")})
        if len(snapshots) == 1:
            raise RuntimeError("ProxyError: connection reset")
        return EmptyFrame()

    with patch.dict(os.environ, {"HTTP_PROXY": "http://proxy", "HTTPS_PROXY": "http://proxy", "ALL_PROXY": "http://proxy"}, clear=False), \
         patch("app.providers._fetch_sector_frame", side_effect=fetch_frame):
        assert fetch_sectors("20260101") == []
        assert snapshots[0]["HTTP_PROXY"] == "http://proxy"
        assert snapshots[1] == {"HTTP_PROXY": None, "HTTPS_PROXY": None, "ALL_PROXY": None}
        assert os.environ["HTTP_PROXY"] == "http://proxy"


def test_sector_fetch_preserves_proxy_and_direct_failures():
    with patch("app.providers._fetch_sector_frame", side_effect=[RuntimeError("ProxyError: proxy down"), RuntimeError("direct DNS failure")]):
        try:
            fetch_sectors("20260101")
        except ProviderError as exc:
            assert "ProxyError: proxy down" in str(exc)
            assert "direct DNS failure" in str(exc)
        else:
            raise AssertionError("Expected the failed proxy retry to raise ProviderError")
