from app.indicators import calculate
from app.services import normalize_signal_filters


def test_calculate_returns_all_requested_technical_metrics():
    closes = [10 + i * 0.1 for i in range(35)]
    metrics = calculate(closes, [x + 0.2 for x in closes], [x - 0.2 for x in closes])
    assert {"macd", "kdj_j", "rsi14", "boll_position", "nine_turn"} <= metrics.keys()
    assert metrics["macd"] > 0
    assert 0 <= metrics["rsi14"] <= 100


def test_signal_filters_accept_every_supported_metric_and_ignore_invalid_values():
    filters = normalize_signal_filters({"min_volume_ratio": "1.2", "max_pb": "5", "min_pct_chg": "bad"})
    assert filters == {"min_volume_ratio": 1.2, "max_pb": 5.0}
