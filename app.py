
import csv
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVAL = "1h"
LIMIT = 300
ACCOUNT_SIZE_USDT = 1000.0
RISK_PER_TRADE = 0.01

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
JOURNAL = DATA_DIR / "signals.csv"

st.set_page_config(page_title="NOVA AI Trader", layout="wide")
st.title("NOVA AI Trader")
st.caption("Тестовая система сигналов. Только демо, без реальных сделок.")


def http_get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "NOVA-AI-Trader/1.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_klines(symbol: str, interval: str = INTERVAL, limit: int = LIMIT):
    params = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    raw = http_get_json(f"https://api.binance.com/api/v3/klines?{params}")
    return [{
        "time": pd.to_datetime(int(x[0]), unit="ms", utc=True),
        "open": float(x[1]),
        "high": float(x[2]),
        "low": float(x[3]),
        "close": float(x[4]),
        "volume": float(x[5]),
    } for x in raw]


def ema(values, period):
    return pd.Series(values).ewm(span=period, adjust=False).mean().iloc[-1]


def rsi(values, period=14):
    s = pd.Series(values)
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    value = 100 - (100 / (1 + rs))
    return float(value.iloc[-1])


def atr(candles, period=14):
    df = pd.DataFrame(candles)
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    return float(tr.ewm(alpha=1/period, adjust=False).mean().iloc[-1])


def analyze(symbol):
    candles = fetch_klines(symbol)
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    price = closes[-1]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200)
    rsi14 = rsi(closes)
    atr14 = atr(candles)
    volume_ratio = volumes[-1] / (sum(volumes[-20:]) / 20)

    score = 0
    reasons = []

    if price > ema20 > ema50 > ema200:
        score += 3
        reasons.append("сильный восходящий тренд")
    elif price < ema20 < ema50 < ema200:
        score -= 3
        reasons.append("сильный нисходящий тренд")
    else:
        score += 1 if price > ema50 else -1
        reasons.append("цена выше EMA50" if price > ema50 else "цена ниже EMA50")

    if 52 <= rsi14 <= 68:
        score += 1
        reasons.append("RSI подтверждает рост")
    elif 32 <= rsi14 <= 48:
        score -= 1
        reasons.append("RSI подтверждает слабость")
    elif rsi14 > 75:
        score -= 1
        reasons.append("рынок перекуплен")
    elif rsi14 < 25:
        score += 1
        reasons.append("рынок перепродан")

    if volume_ratio >= 1.3:
        if price > ema20:
            score += 1
            reasons.append("рост подтвержден объемом")
        else:
            score -= 1
            reasons.append("падение подтверждено объемом")

    action = "BUY" if score >= 4 else "SELL" if score <= -4 else "WAIT"

    if action == "BUY":
        stop = price - 1.5 * atr14
        take = price + 3.0 * atr14
    elif action == "SELL":
        stop = price + 1.5 * atr14
        take = price - 3.0 * atr14
    else:
        stop = take = None

    position_size = 0
    if stop:
        position_size = (ACCOUNT_SIZE_USDT * RISK_PER_TRADE) / abs(price - stop)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "action": action,
        "price": round(price, 4),
        "stop": round(stop, 4) if stop else None,
        "take": round(take, 4) if take else None,
        "position_size": round(position_size, 6),
        "confidence": min(95, 50 + abs(score) * 9),
        "score": score,
        "rsi14": round(rsi14, 2),
        "ema20": round(ema20, 4),
        "ema50": round(ema50, 4),
        "ema200": round(ema200, 4),
        "volume_ratio": round(volume_ratio, 2),
        "reason": "; ".join(reasons),
        "candles": candles,
    }


def save_signal(signal):
    row = {k: v for k, v in signal.items() if k != "candles"}
    exists = JOURNAL.exists()
    with JOURNAL.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def send_telegram(text):
    token = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20):
        pass


if st.button("Обновить сигналы", type="primary"):
    results = []
    for symbol in SYMBOLS:
        try:
            result = analyze(symbol)
            save_signal(result)
            results.append(result)
        except Exception as e:
            st.error(f"{symbol}: {e}")

    if results:
        message_parts = []
        for r in results:
            st.subheader(r["symbol"])
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Сигнал", r["action"])
            c2.metric("Цена", r["price"])
            c3.metric("Уверенность", f'{r["confidence"]}%')
            c4.metric("RSI", r["rsi14"])

            if r["action"] != "WAIT":
                st.write(f'**Стоп:** {r["stop"]} | **Тейк:** {r["take"]} | **Размер позиции:** {r["position_size"]}')
            st.write(r["reason"])

            df = pd.DataFrame(r["candles"]).set_index("time")
            st.line_chart(df[["close"]])

            message_parts.append(
                f'{r["symbol"]} | {r["action"]}\n'
                f'Цена: {r["price"]}\n'
                f'Стоп: {r["stop"] or "-"}\n'
                f'Тейк: {r["take"] or "-"}\n'
                f'Уверенность: {r["confidence"]}%\n'
                f'Причина: {r["reason"]}'
            )

        try:
            send_telegram("\n\n".join(message_parts))
        except Exception as e:
            st.warning(f"Telegram не отправлен: {e}")

st.divider()
st.subheader("Журнал")
if JOURNAL.exists():
    journal = pd.read_csv(JOURNAL)
    st.dataframe(journal.tail(100), use_container_width=True)
else:
    st.info("Журнал появится после первого запуска.")
