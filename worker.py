# -*- coding: utf-8 -*-
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

COINS_COUNT = 150
START_BALANCE = 1000.0
MAX_POSITIONS = 10
POSITION_SHARE = 0.05
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "portfolio.json"
TRADES_FILE = DATA_DIR / "trades.csv"
SIGNALS_FILE = DATA_DIR / "latest_signals.csv"
EQUITY_FILE = DATA_DIR / "equity.csv"


def get_json(url, retries=3):
    headers = {
        "User-Agent": "Mozilla/5.0 NOVA-AI-Trader",
        "Accept": "application/json",
    }
    last_error = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=35) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Источник данных недоступен: {last_error}")


def fetch_market():
    params = urllib.parse.urlencode({
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": COINS_COUNT,
        "page": 1,
        "sparkline": "true",
        "price_change_percentage": "1h,24h,7d,30d",
    })
    data = get_json(f"https://api.coingecko.com/api/v3/coins/markets?{params}")
    if not isinstance(data, list):
        raise RuntimeError("CoinGecko вернул неожиданный ответ")
    return data


def fetch_fear_greed():
    try:
        item = get_json("https://api.alternative.me/fng/?limit=1")["data"][0]
        return int(item["value"])
    except Exception:
        return 50


def safe(value, default=0.0):
    return float(value) if value is not None else default


def trend_score(prices):
    if not prices or len(prices) < 24:
        return 0
    series = pd.Series(prices, dtype="float64")
    short = series.tail(24).mean()
    long = series.tail(72).mean() if len(series) >= 72 else series.mean()
    if short > long * 1.01:
        return 1
    if short < long * 0.99:
        return -1
    return 0


def evaluate(coin, fear_value):
    ch1h = safe(coin.get("price_change_percentage_1h_in_currency"))
    ch24 = safe(coin.get("price_change_percentage_24h_in_currency"))
    ch7d = safe(coin.get("price_change_percentage_7d_in_currency"))
    ch30 = safe(coin.get("price_change_percentage_30d_in_currency"))

    market_cap = safe(coin.get("market_cap"))
    volume = safe(coin.get("total_volume"))
    volume_ratio = volume / market_cap if market_cap > 0 else 0.0

    current = safe(coin.get("current_price"))
    high24 = safe(coin.get("high_24h"))
    low24 = safe(coin.get("low_24h"))
    range24 = (high24 - low24) / current * 100 if current > 0 else 0.0

    sparkline = (coin.get("sparkline_in_7d") or {}).get("price") or []
    trend7 = trend_score(sparkline)

    score = 50.0
    reasons = []

    for value, weight, label in [
        (ch1h, 1.0, "1ч"),
        (ch24, 1.5, "24ч"),
        (ch7d, 0.7, "7д"),
        (ch30, 0.25, "30д"),
    ]:
        score += max(-12, min(12, value * weight))
        if abs(value) >= 2:
            reasons.append(f"{label}: {value:+.1f}%")

    if volume_ratio >= 0.15:
        score += 8
        reasons.append("высокая ликвидность")
    elif volume_ratio >= 0.05:
        score += 4
        reasons.append("нормальная ликвидность")
    elif volume_ratio < 0.01:
        score -= 10
        reasons.append("низкая ликвидность")

    score += trend7 * 8
    if trend7 > 0:
        reasons.append("7-дневный тренд вверх")
    elif trend7 < 0:
        reasons.append("7-дневный тренд вниз")

    if range24 > 20:
        score -= 14
        reasons.append("экстремальная волатильность")
    elif range24 > 10:
        score -= 7
        reasons.append("высокая волатильность")

    if fear_value >= 70:
        score += 3 if ch24 > 0 else -3
    elif fear_value <= 30:
        score -= 3 if ch24 < 0 else 2

    nova_score = int(max(0, min(100, round(score))))

    if nova_score >= 75 and ch24 > 0 and trend7 >= 0 and volume_ratio >= 0.01:
        signal = "BUY"
    elif nova_score <= 25 and ch24 < 0 and trend7 <= 0 and volume_ratio >= 0.01:
        signal = "SELL"
    else:
        signal = "WAIT"

    risk = (
        "Высокий"
        if range24 > 12 or market_cap < 100_000_000
        else "Средний"
        if range24 > 6
        else "Низкий"
    )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "coin": coin.get("name"),
        "symbol": str(coin.get("symbol", "")).upper(),
        "rank": coin.get("market_cap_rank"),
        "signal": signal,
        "nova_score": nova_score,
        "price": current,
        "change_24h": round(ch24, 2),
        "change_7d": round(ch7d, 2),
        "volume_ratio": round(volume_ratio, 4),
        "risk": risk,
        "reason": "; ".join(reasons[:5]) or "нет сильного преимущества",
    }


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"balance": START_BALANCE, "positions": {}}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_csv(path, row):
    frame = pd.DataFrame([row])
    if path.exists():
        frame.to_csv(path, mode="a", header=False, index=False)
    else:
        frame.to_csv(path, index=False)


def close_position(state, symbol, price, reason):
    position = state["positions"][symbol]

    if position["side"] == "BUY":
        pnl = (price - position["entry"]) * position["qty"]
    else:
        pnl = (position["entry"] - price) * position["qty"]

    state["balance"] += position["allocated"] + pnl

    append_csv(TRADES_FILE, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "type": "CLOSE",
        "side": position["side"],
        "price": round(price, 8),
        "qty": round(position["qty"], 8),
        "pnl": round(pnl, 4),
        "reason": reason,
        "balance": round(state["balance"], 2),
    })

    del state["positions"][symbol]


def open_position(state, row):
    symbol = row["symbol"]

    if symbol in state["positions"]:
        return
    if len(state["positions"]) >= MAX_POSITIONS:
        return

    allocation = state["balance"] * POSITION_SHARE
    if allocation < 10:
        return

    price = float(row["price"])
    side = row["signal"]
    qty = allocation / price

    stop = price * (1 - STOP_LOSS_PCT) if side == "BUY" else price * (1 + STOP_LOSS_PCT)
    take = price * (1 + TAKE_PROFIT_PCT) if side == "BUY" else price * (1 - TAKE_PROFIT_PCT)

    state["balance"] -= allocation
    state["positions"][symbol] = {
        "name": row["coin"],
        "side": side,
        "entry": price,
        "qty": qty,
        "allocated": allocation,
        "stop": stop,
        "take": take,
        "score": int(row["nova_score"]),
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }

    append_csv(TRADES_FILE, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "type": "OPEN",
        "side": side,
        "price": round(price, 8),
        "qty": round(qty, 8),
        "pnl": 0.0,
        "reason": f"NOVA Score {row['nova_score']}",
        "balance": round(state["balance"], 2),
    })


def main():
    DATA_DIR.mkdir(exist_ok=True)

    market = fetch_market()
    fear_value = fetch_fear_greed()
    rows = [evaluate(coin, fear_value) for coin in market]
    signals = pd.DataFrame(rows)
    signals.to_csv(SIGNALS_FILE, index=False)

    state = load_state()
    prices = dict(zip(signals["symbol"], signals["price"]))

    for symbol in list(state["positions"].keys()):
        if symbol not in prices:
            continue

        price = float(prices[symbol])
        position = state["positions"][symbol]

        if position["side"] == "BUY":
            if price <= position["stop"]:
                close_position(state, symbol, price, "STOP")
            elif price >= position["take"]:
                close_position(state, symbol, price, "TAKE")
        else:
            if price >= position["stop"]:
                close_position(state, symbol, price, "STOP")
            elif price <= position["take"]:
                close_position(state, symbol, price, "TAKE")

    candidates = signals[
        signals["signal"].isin(["BUY", "SELL"])
        & signals["risk"].isin(["Низкий", "Средний"])
    ].sort_values("nova_score", ascending=False)

    for _, row in candidates.iterrows():
        if len(state["positions"]) >= MAX_POSITIONS:
            break
        open_position(state, row)

    equity = state["balance"]

    for symbol, position in state["positions"].items():
        price = float(prices.get(symbol, position["entry"]))

        if position["side"] == "BUY":
            pnl = (price - position["entry"]) * position["qty"]
        else:
            pnl = (position["entry"] - price) * position["qty"]

        equity += position["allocated"] + pnl

    append_csv(EQUITY_FILE, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "equity": round(equity, 2),
        "balance": round(state["balance"], 2),
        "open_positions": len(state["positions"]),
    })

    save_state(state)
    print(f"NOVA cycle complete. Equity={equity:.2f}; positions={len(state['positions'])}")


if __name__ == "__main__":
    main()
