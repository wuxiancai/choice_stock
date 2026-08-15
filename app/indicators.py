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


def calculate(
    closes: list[float], highs: list[float], lows: list[float], volumes: list[float] | None = None,
) -> dict[str, float | int | None]:
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
    bbi = sum(sum(closes[-period:]) / period for period in (3, 6, 12, 24)) / 4
    bias_base = sum(closes[-6:]) / 6
    bias = (closes[-1] - bias_base) * 100 / bias_base if bias_base else None
    psy = sum(closes[index] > closes[index - 1] for index in range(len(closes) - 12, len(closes))) * 100 / 12

    vr = None
    if volumes is not None and len(volumes) == len(closes) and len(closes) >= 27 and all(volume is not None for volume in volumes[-27:]):
        up_volume = down_volume = 0.0
        for index in range(len(closes) - 26, len(closes)):
            volume = volumes[index]
            if closes[index] > closes[index - 1]:
                up_volume += volume
            elif closes[index] < closes[index - 1]:
                down_volume += volume
            else:
                up_volume += volume / 2
                down_volume += volume / 2
        vr = up_volume * 100 / down_volume if down_volume else None

    dx_values = []
    for end in range(14, len(closes)):
        true_range = plus_dm = minus_dm = 0.0
        for index in range(end - 13, end + 1):
            upward_move = highs[index] - highs[index - 1]
            downward_move = lows[index - 1] - lows[index]
            plus_dm += upward_move if upward_move > downward_move and upward_move > 0 else 0
            minus_dm += downward_move if downward_move > upward_move and downward_move > 0 else 0
            true_range += max(
                highs[index] - lows[index], abs(highs[index] - closes[index - 1]), abs(lows[index] - closes[index - 1]),
            )
        if true_range:
            plus_di, minus_di = plus_dm * 100 / true_range, minus_dm * 100 / true_range
            directional_total = plus_di + minus_di
            if directional_total:
                dx_values.append(abs(plus_di - minus_di) * 100 / directional_total)
    dmi = sum(dx_values[-14:]) / 14 if len(dx_values) >= 14 else None
    return {
        "macd": macd, "rsi14": rsi, "boll_position": boll_position, "kdj_j": j, "nine_turn": nine_turn,
        "bbi": bbi, "bias": bias, "vr": vr, "psy": psy, "dmi": dmi,
    }
