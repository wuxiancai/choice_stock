from app.indicators import calculate
from app.providers import ProviderError, SectorFetchResult, fetch_quotes, fetch_sector_history, fetch_sectors, fetch_tushare_sector_history
from app.services import dashboard, format_cny, format_datetime, format_sector_date, format_trade_date, normalize_signal_filters, recent_system_errors, record_system_error, sector_source_summary, signal_tones, sync_latest
from app.config import settings
from app.database import connect, initialize
from jinja2 import Environment, FileSystemLoader
from unittest.mock import patch
import os


def test_calculate_returns_all_requested_technical_metrics():
    closes = [10 + i * 0.1 + (0.3 if i % 2 else 0) for i in range(40)]
    metrics = calculate(closes, [x + 0.2 for x in closes], [x - 0.2 for x in closes], [1000 + i * 10 for i in range(40)])
    assert {"macd", "kdj_j", "rsi14", "boll_position", "nine_turn", "bbi", "bias", "vr", "psy", "dmi"} <= metrics.keys()
    assert metrics["macd"] > 0
    assert 0 <= metrics["rsi14"] <= 100
    assert all(metrics[key] is not None for key in ("bbi", "bias", "vr", "psy", "dmi"))


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


def test_format_sector_date_is_compact_without_a_year():
    assert format_sector_date("20260814") == "8.14"
    assert format_sector_date("20260104") == "1.04"


def test_signal_tones_marks_bullish_values_green_and_risks_yellow():
    tones = signal_tones({
        "score": 75, "nine_turn": 8, "pct_chg": -1, "main_net_inflow": 1,
        "pe": 12, "volume_ratio": 6, "turnover_rate": 8, "macd": 1,
        "kdj_j": 110, "rsi14": 20, "boll_position": 1.1, "close": 10, "bbi": 9,
        "bias": 7, "vr": 50, "psy": 80, "dmi": 30, "pb": 0.8,
    })
    assert tones["macd"] == tones["rsi14"] == tones["bbi"] == "positive"
    assert tones["kdj_j"] == tones["nine_turn"] == tones["bias"] == "risk"
    assert tones["amount"] == tones["total_mv"] == tones["dmi"] == "neutral"


def test_standard_display_time_formats_date_and_shanghai_time():
    assert format_trade_date("20280814") == "2028-08-14"
    assert format_datetime("2028-08-14T06:35:03+00:00") == "2028-08-14 14:35:03"


def test_signal_filters_accept_supported_metrics_and_ignore_removed_metrics():
    filters = normalize_signal_filters({"stock_code": " 000001.sz ", "min_volume_ratio": "1.2", "max_pb": "5", "min_bbi": "10", "max_dmi": "30", "min_score": "50", "max_total_mv": "1000", "min_macd": "0", "min_pct_chg": "2"})
    assert filters == {"stock_code": "000001.SZ", "min_volume_ratio": 1.2, "max_pb": 5.0, "min_bbi": 10.0, "max_dmi": 30.0}


def test_sector_source_summary_distinguishes_failed_and_unattempted_sources():
    summary = sector_source_summary("tushare_moneyflow", [])
    assert "当日行业数据：Tushare 申万一级行业聚合 成功" in summary
    assert "未尝试：腾讯申万一级行业聚合（前序来源已成功）" in summary
    assert "东方财富" not in summary
    assert "同花顺" not in summary


def test_dashboard_template_renders_historical_signal_with_new_nullable_fields():
    environment = Environment(loader=FileSystemLoader("app/templates"))
    environment.filters["cny"] = format_cny
    environment.filters["trade_date"] = format_trade_date
    environment.filters["sector_date"] = format_sector_date
    environment.filters["datetime"] = format_datetime
    template = environment.get_template("index.html")
    signal = {
        "name": "测试", "industry": "银行", "ts_code": "000001.SZ", "score": 0, "macd": 0, "kdj_j": 0,
        "rsi14": 0, "boll_position": 0, "nine_turn": 4, "bbi": None, "bias": None,
        "vr": None, "psy": None, "dmi": None, "volume_ratio": None,
        "turnover_rate": None, "amount": 100, "total_mv": None, "pe": None, "pb": None,
        "pct_chg": None, "main_net_inflow": None, "reasons": "[]",
    }
    signal["tones"] = signal_tones(signal)
    html = template.render(dashboard={"run": None, "dates": [], "sector_dates": ["20260814"], "sector_snapshot_dates": [], "sectors": [], "signals": [signal], "filters": {}, "system_errors": []})
    assert "000001.SZ" in html
    assert ">银行</td>" in html
    assert "—" in html
    assert ">4</td>" in html
    assert 'id="signal-table"' in html
    assert 'id="sync-button"' in html
    assert "数据研究用途，不构成投资建议。每日 21:00（上海时区）自动分析。" not in html
    assert "A股盘后选股" not in html
    assert 'id="sector-table-wrap"' in html
    assert 'id="sector-table"' in html
    assert "#sector-table{width:auto;table-layout:auto}" in html
    assert ">8.14</th>" in html
    assert "2026-08-14</th>" not in html
    assert "event.preventDefault()" in html
    assert "'#' ~ row.daily_ranks" not in html
    assert "repeat(8,minmax(0,1fr))" in html
    assert 'class="sortable nine-turn-header"' in html
    assert 'class="nine-turn-filter"' in html
    assert ">净流入</th>" in html
    assert "主力净流入<br><small>大单+特大单</small>" not in html
    assert "主力净流入（大单+特大单，元）" not in html
    assert 'data-sort-type="number"' in html
    filter_section = html.split('<div class="card"><h2>当日技术信号</h2>', 1)[0]
    assert 'name="stock_code"' in filter_section
    for field in ("macd", "kdj_j", "rsi14", "boll_position", "pct_chg"):
        assert f'name="min_{field}"' not in filter_section
    for field in ("score", "total_mv"):
        assert f'name="min_{field}"' not in filter_section
    for field in ("bbi", "bias", "vr", "psy", "dmi"):
        assert f'name="min_{field}"' in filter_section
    headers = html.split('<table id="signal-table">', 1)[1].split("</thead>", 1)[0]
    assert headers.index("评分") < headers.index("九转") < headers.index("涨跌幅")
    assert headers.index("股票") < headers.index("行业") < headers.index("评分")
    assert headers.index("净流入") < headers.index("PE<br><small>TTM</small>") < headers.index("量比") < headers.index("换手率") < headers.index("MACD")
    assert headers.index("BBI") < headers.index("BIAS") < headers.index("VR") < headers.index("PSY") < headers.index("DMI")


def test_dashboard_filters_signals_by_stock_code_prefix(tmp_path):
    original_data_dir = settings.data_dir
    object.__setattr__(settings, "data_dir", tmp_path)
    try:
        initialize()
        with connect() as conn:
            conn.execute("INSERT INTO sync_runs(started_at,trade_date,status) VALUES (?,?,?)", ("now", "20260813", "success"))
            conn.executemany(
                "INSERT INTO stock_signals(trade_date,ts_code,name,score,macd,kdj_j,rsi14,boll_position,nine_turn,reasons,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("20260813", "000001.SZ", "平安银行", 80, 0, 0, 0, 0, 0, "[]", "test"),
                    ("20260813", "600000.SH", "浦发银行", 70, 0, 0, 0, 0, 0, "[]", "test"),
                ],
            )
        result = dashboard({"stock_code": "000001"})
        assert result["filters"] == {"stock_code": "000001"}
        assert [row["ts_code"] for row in result["signals"]] == ["000001.SZ"]
        result = dashboard({"stock_code": "平安"})
        assert [row["ts_code"] for row in result["signals"]] == ["000001.SZ"]
    finally:
        object.__setattr__(settings, "data_dir", original_data_dir)


def test_dashboard_sorts_signals_by_main_net_inflow_descending(tmp_path):
    original_data_dir = settings.data_dir
    object.__setattr__(settings, "data_dir", tmp_path)
    try:
        initialize()
        with connect() as conn:
            conn.execute("INSERT INTO sync_runs(started_at,trade_date,status) VALUES (?,?,?)", ("now", "20260813", "success"))
            conn.executemany(
                "INSERT INTO stock_signals(trade_date,ts_code,name,score,macd,kdj_j,rsi14,boll_position,nine_turn,main_net_inflow,reasons,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("20260813", "000001.SZ", "高流入", 10, 0, 0, 0, 0, 0, 300, "[]", "test"),
                    ("20260813", "000002.SZ", "低流入", 90, 0, 0, 0, 0, 0, 100, "[]", "test"),
                    ("20260813", "000003.SZ", "缺失", 100, 0, 0, 0, 0, 0, None, "[]", "test"),
                ],
            )
        result = dashboard()
        assert [row["name"] for row in result["signals"]] == ["高流入", "低流入", "缺失"]
    finally:
        object.__setattr__(settings, "data_dir", original_data_dir)


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
        assert result["sector_snapshot_dates"] == []
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
            patch("app.services.fetch_sectors", return_value=SectorFetchResult(latest_rows, "tushare_moneyflow", [])), \
             patch("app.services.fetch_sector_history", return_value=(history_rows, [])) as history_fetch:
            result = sync_latest()
        assert result["status"] == "success"
        assert "当日行业数据：Tushare 申万一级行业聚合 成功" in result["message"]
        assert "Tushare 同花顺" not in result["message"]
        history_fetch.assert_called_once_with(history_dates, [f"行业{index}" for index in range(30)])
        with connect() as conn:
            persisted_dates = [row[0] for row in conn.execute("SELECT DISTINCT trade_date FROM sector_snapshots ORDER BY trade_date")]
        assert persisted_dates == dates[-5:]
    finally:
        object.__setattr__(settings, "data_dir", original_data_dir)


def test_sync_repairs_partial_sector_history_dates(tmp_path):
    original_data_dir = settings.data_dir
    object.__setattr__(settings, "data_dir", tmp_path)
    dates = [f"202601{i:02d}" for i in range(1, 91)]
    latest_date, history_dates = dates[-1], dates[-5:-1]
    latest_rows = [
        {"trade_date": latest_date, "sector_code": f"s{index}", "sector_name": f"行业{index}", "pct_chg": 1.0, "amount": 100, "main_net_inflow": 100, "source": "ths"}
        for index in range(30)
    ]
    history_rows = [
        {"trade_date": trade_date, "sector_code": f"s{index}", "sector_name": f"行业{index}", "pct_chg": 1.0, "amount": 100, "main_net_inflow": 100, "source": "eastmoney_history"}
        for trade_date in history_dates for index in range(30)
    ]
    try:
        initialize()
        with connect() as conn:
            conn.execute("INSERT INTO sector_snapshots VALUES (?,?,?,?,?,?,?)", (history_dates[0], "s0", "行业0", 1.0, 100, 100, "old"))
        with patch("app.services.recent_trade_dates", return_value=dates), \
             patch("app.services.fetch_quotes", return_value=[]), \
             patch("app.services.fetch_sectors", return_value=SectorFetchResult(latest_rows, "ths", [])), \
             patch("app.services.fetch_sector_history", return_value=(history_rows, [])) as history_fetch:
            sync_latest()
        history_fetch.assert_called_once_with(history_dates, [f"行业{index}" for index in range(30)])
    finally:
        object.__setattr__(settings, "data_dir", original_data_dir)


def test_sector_fetch_uses_l1_tencent_when_tushare_l1_aggregation_fails():
    tencent_rows = [
        {"trade_date": "20260101", "sector_code": f"i{index}", "sector_name": f"腾讯行业{index}",
         "pct_chg": 1.0, "amount": 100, "main_net_inflow": 100, "source": "tencent"}
        for index in range(30)
    ]
    with patch("app.providers._fetch_tushare_sector_rows", side_effect=ProviderError("unavailable")), \
         patch("app.providers._fetch_tencent_sector_rows", return_value=tencent_rows):
        result = fetch_sectors("20260101")
    assert result.source == "tencent"
    assert result.rows == tencent_rows
    assert len(result.fallback_errors) == 1


def test_sector_sources_do_not_include_tushare_industry_flow_endpoints():
    """A 3,000-point account must never probe the higher-tier industry APIs."""
    assert "moneyflow_ind_ths" not in __import__("app.providers", fromlist=["*"]).__dict__
    assert "moneyflow_ind_dc" not in __import__("app.providers", fromlist=["*"]).__dict__


def test_tushare_quote_main_flow_uses_big_plus_extra_orders_and_dynamic_pe_ttm():
    import pandas as pd

    class Pro:
        def daily(self, **_):
            return pd.DataFrame([{"ts_code": "000001.SZ", "open": 1, "high": 1, "low": 1, "close": 1, "pct_chg": 0, "vol": 1, "amount": 1}])

        def moneyflow(self, **_):
            return pd.DataFrame([{
                "ts_code": "000001.SZ", "buy_lg_amount": 20, "sell_lg_amount": 5,
                "buy_elg_amount": 30, "sell_elg_amount": 8,
            }])

        def stock_basic(self, **_):
            return pd.DataFrame({"ts_code": ["000001.SZ"], "name": ["测试"], "industry": ["银行"]})

        def daily_basic(self, **kwargs):
            self.daily_basic_fields = kwargs["fields"]
            return pd.DataFrame([{"ts_code": "000001.SZ", "turnover_rate": 1, "volume_ratio": 2, "pe": 9, "pe_ttm": 12.5, "pb": 1, "total_mv": 3}])

    pro = Pro()
    with patch("app.providers._ts", return_value=pro), \
         patch("app.providers.sw_l1_industry_by_code", return_value={"000001.SZ": "银行"}):
        rows = fetch_quotes("20260814")

    assert rows[0]["main_net_inflow"] == 370_000
    assert rows[0]["industry"] == "银行"
    assert rows[0]["pe"] == 12.5
    assert "pe_ttm" in pro.daily_basic_fields
    assert ",pe," not in pro.daily_basic_fields


def test_sector_history_does_not_mix_non_l1_eastmoney_boards():
    with patch("app.providers.fetch_tushare_sector_history", return_value=([], ["20260810", "20260811"], [])):
        rows, diagnostics = fetch_sector_history(["20260810", "20260811"], ["银行"])
    assert rows == []
    assert "未使用东方财富/同花顺/腾讯的非一级行业数据" in diagnostics[-1]


def test_tushare_individual_moneyflow_aggregates_real_industry_history():
    import pandas as pd

    class Pro:
        def moneyflow(self, **_):
            return pd.DataFrame({
                "ts_code": ["000001.SZ", "000002.SZ"],
                "buy_lg_amount": [2, 3], "sell_lg_amount": [1, 1],
                "buy_elg_amount": [10, 5], "sell_elg_amount": [3, 7],
            })

        def daily(self, **_):
            return pd.DataFrame({"ts_code": ["000001.SZ", "000002.SZ"], "pct_chg": [1.0, -1.0], "amount": [100, 300]})

    with patch("app.providers._ts", return_value=Pro()), \
         patch("app.providers.sw_l1_industry_by_code", return_value={"000001.SZ": "银行", "000002.SZ": "银行"}), \
         patch("app.providers.MIN_VALID_SECTOR_ROWS", 1):
        rows, unresolved, diagnostics = fetch_tushare_sector_history(["20260810"])

    assert unresolved == []
    assert rows == [{"trade_date": "20260810", "sector_code": "银行", "sector_name": "银行", "pct_chg": -0.5, "amount": 80_000.0, "main_net_inflow": 80_000.0, "source": "tushare_sw_l1_moneyflow_aggregate"}]
    assert "成功（1 个行业）" in diagnostics[0]
