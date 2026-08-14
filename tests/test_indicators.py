from app.indicators import calculate


def test_calculate_returns_all_requested_technical_metrics():
    closes = [10 + i * 0.1 for i in range(35)]
    metrics = calculate(closes, [x + 0.2 for x in closes], [x - 0.2 for x in closes])
    assert {"macd", "kdj_j", "rsi14", "boll_position", "nine_turn"} <= metrics.keys()
    assert metrics["macd"] > 0
    assert 0 <= metrics["rsi14"] <= 100
