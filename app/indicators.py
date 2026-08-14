from __future__ import annotations

import math


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    result = values[0]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result = value * alpha + result * (1 - alpha)
    return result


def calculate(closes: list[float], highs: list[float], lows: list[float]) -> dict[str, float | int]:
    if len(closes) < 26:
        raise ValueError("至少需要 26 个交易日")
    fast = ema(closes[-35:], 12)
    slow = ema(closes[-35:], 26)
    macd = fast - slow
    rsi_gains = [max(closes[i] - closes[i - 1], 0) for i in range(-14, 0)]
    rsi_losses = [max(closes[i - 1] - closes[i], 0) for i in range(-14, 0)]
    avg_gain, avg_loss = sum(rsi_gains) / 14, sum(rsi_losses) / 14
    rsi = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    window = closes[-20:]
    mid = sum(window) / 20
    std = math.sqrt(sum((x - mid) ** 2 for x in window) / 20)
    upper, lower = mid + 2 * std, mid - 2 * std
    boll_position = 0.5 if upper == lower else (closes[-1] - lower) / (upper - lower)
    hh, ll = max(highs[-9:]), min(lows[-9:])
    rsv = 50 if hh == ll else (closes[-1] - ll) * 100 / (hh - ll)
    k = (2 * 50 + rsv) / 3
    d = (2 * 50 + k) / 3
    j = 3 * k - 2 * d
    upward_turns = 0
    downward_turns = 0
    for i in range(len(closes) - 1, max(len(closes) - 10, 3), -1):
        if closes[i] > closes[i - 4]:
            upward_turns += 1
        else:
            break
    for i in range(len(closes) - 1, max(len(closes) - 10, 3), -1):
        if closes[i] < closes[i - 4]:
            downward_turns += 1
        else:
            break
    # 正数表示连续收盘价高于四日前（高位卖出提示），负数表示连续低于四日前（低位买入提示）。
    nine_turn = upward_turns if upward_turns else -downward_turns
    return {"macd": macd, "rsi14": rsi, "boll_position": boll_position, "kdj_j": j, "nine_turn": nine_turn}
