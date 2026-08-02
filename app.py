import csv
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

SYMBOLS = {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana"}
INTERVAL = "1h"
RANGE = "1mo"
ACCOUNT_SIZE_USDT = 1000.0
RISK_PER_TRADE = 0.01

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
JOURNAL = DATA_DIR / "signals.csv"

st.set_page_config(page_title="NOVA AI Trader", layout="wide")
st.title("NOVA AI Trader")
st.caption("Тестовая система сигналов. Только демо, без реальных сделок.")

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))

def fetch_yahoo(symbol):
    encoded = urllib.parse.quote(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval={INTERVAL}&range={RANGE}&includePrePost=false"
    result = get_json(url)["chart"]["result"][0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    candles = []
    for i, ts in enumerate(timestamps):
        vals = [quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i], quote["volume"][i]]
        if any(v is None for v in vals):
            continue
        candles.append({
            "time": pd.to_datetime(ts, unit="s", utc=True),
            "open": float(vals[0]),
            "high": float(vals[1]),
            "low": float(vals[2]),
            "close": float(vals[3]),
            "volume": float(vals[4]),
        })
    if len(candles) < 210:
        raise RuntimeError("Недостаточно данных")
    return candles

def ema(values, period):
    return float(pd.Series(values).ewm(span=period, adjust=False).mean().iloc[-1])

def rsi(values, period=14):
    s = pd.Series(values)
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return float((100 - (100 / (1 + rs))).iloc[-1])

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
    candles = fetch_yahoo(symbol)
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    price = closes[-1]
    ema20, ema50, ema200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    rsi14 = rsi(closes)
    atr14 = atr(candles)
    avg_volume = sum(volumes[-20:]) / 20 if sum(volumes[-20:]) else 1
    volume_ratio = volumes[-1] / avg_volume

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
        stop, take = price - 1.5 * atr14, price + 3.0 * atr14
    elif action == "SELL":
        stop, take = price + 1.5 * atr14, price - 3.0 * atr14
    else:
        stop = take = None

    position_size = 0
    if stop is not None:
        position_size = (ACCOUNT_SIZE_USDT * RISK_PER_TRADE) / abs(price - stop)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "name": SYMBOLS[symbol],
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

if st.button("Обновить сигналы", type="primary"):
    for symbol in SYMBOLS:
        try:
            result = analyze(symbol)
            save_signal(result)
            st.subheader(f'{result["name"]} ({result["symbol"]})')
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Сигнал", result["action"])
            c2.metric("Цена", result["price"])
            c3.metric("Уверенность", f'{result["confidence"]}%')
            c4.metric("RSI", result["rsi14"])
            if result["action"] != "WAIT":
                st.write(f'**Стоп:** {result["stop"]} | **Тейк:** {result["take"]} | **Размер позиции:** {result["position_size"]}')
            st.write(result["reason"])
            df = pd.DataFrame(result["candles"]).set_index("time")
            st.line_chart(df[["close"]])
        except Exception as e:
            st.error(f"{symbol}: {e}")

st.divider()
st.subheader("Журнал")
if JOURNAL.exists():
    st.dataframe(pd.read_csv(JOURNAL).tail(100), use_container_width=True)
else:
    st.info("Журнал появится после первого запуска.")
