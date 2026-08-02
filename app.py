import json
import urllib.parse
import urllib.request

import pandas as pd
import streamlit as st

SYMBOLS = {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana"}
INTERVAL = "1h"
RANGE = "3mo"

st.set_page_config(page_title="NOVA AI Trader", layout="wide")
st.title("NOVA AI Trader — Stable")
st.caption("Стабильная бесплатная версия. Реальные деньги не подключены.")

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))

@st.cache_data(ttl=900)
def fetch_yahoo(symbol):
    encoded = urllib.parse.quote(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval={INTERVAL}&range={RANGE}&includePrePost=false"
    result = get_json(url)["chart"]["result"][0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]

    rows = []
    for i, ts in enumerate(timestamps):
        vals = [quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i], quote["volume"][i]]
        if any(v is None for v in vals):
            continue
        rows.append({
            "time": pd.to_datetime(ts, unit="s", utc=True),
            "open": float(vals[0]),
            "high": float(vals[1]),
            "low": float(vals[2]),
            "close": float(vals[3]),
            "volume": float(vals[4]),
        })

    df = pd.DataFrame(rows).set_index("time")
    if len(df) < 250:
        raise RuntimeError("Недостаточно данных")
    return df

@st.cache_data(ttl=3600)
def fetch_fear_greed():
    try:
        item = get_json("https://api.alternative.me/fng/?limit=1")["data"][0]
        return int(item["value"]), item["value_classification"]
    except Exception:
        return 50, "Neutral"

def indicators(df):
    x = df.copy()
    x["ema20"] = x["close"].ewm(span=20, adjust=False).mean()
    x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()
    x["ema200"] = x["close"].ewm(span=200, adjust=False).mean()

    delta = x["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    x["rsi"] = 100 - (100 / (1 + rs))

    x["macd"] = x["close"].ewm(span=12, adjust=False).mean() - x["close"].ewm(span=26, adjust=False).mean()
    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()

    prev_close = x["close"].shift(1)
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    x["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()

    direction = x["close"].diff().apply(lambda v: 1 if v > 0 else -1 if v < 0 else 0)
    x["obv"] = (direction * x["volume"]).fillna(0).cumsum()
    x["high20"] = x["high"].rolling(20).max()
    x["low20"] = x["low"].rolling(20).min()
    return x.dropna()

def analyze(df, fear_value):
    row = df.iloc[-1]
    prev20 = df.tail(20)
    price = float(row["close"])

    agents = []
    trend = 2 if price > row["ema20"] > row["ema50"] > row["ema200"] else -2 if price < row["ema20"] < row["ema50"] < row["ema200"] else 1 if price > row["ema50"] else -1
    agents.append(("Trend", trend, "EMA20/50/200"))

    momentum = 1 if row["macd"] > row["macd_signal"] else -1
    momentum += 1 if 52 <= row["rsi"] <= 68 else -1 if 32 <= row["rsi"] <= 48 else 0
    agents.append(("Momentum", momentum, f"RSI {row['rsi']:.1f}, MACD"))

    volume = 1 if row["obv"] > prev20["obv"].iloc[0] else -1
    agents.append(("Volume", volume, "OBV"))

    price_action = 2 if price >= row["high20"] * 0.995 else -2 if price <= row["low20"] * 1.005 else 1 if price > row["ema20"] else -1
    agents.append(("Price Action", price_action, "Уровни и EMA20"))

    atr_pct = float(row["atr"] / price * 100)
    risk = -2 if atr_pct > 3.5 else -1 if atr_pct > 2 else 1
    agents.append(("Risk", risk, f"ATR {atr_pct:.2f}%"))

    sentiment = 1 if fear_value >= 60 else -1 if fear_value <= 40 else 0
    agents.append(("Sentiment", sentiment, f"Fear & Greed {fear_value}"))

    score = trend + momentum + volume + price_action + sentiment

    if risk <= -2:
        action = "WAIT"
    elif score >= 5:
        action = "BUY"
    elif score <= -5:
        action = "SELL"
    else:
        action = "WAIT"

    atr_value = float(row["atr"])
    stop = price - 1.5 * atr_value if action == "BUY" else price + 1.5 * atr_value if action == "SELL" else None
    take = price + 3 * atr_value if action == "BUY" else price - 3 * atr_value if action == "SELL" else None
    agreement = abs(score) / max(sum(abs(a[1]) for a in agents if a[0] != "Risk"), 1)
    confidence = round(min(95, 50 + agreement * 45))
    return action, price, stop, take, confidence, score, agents

fear_value, fear_label = fetch_fear_greed()
st.metric("Fear & Greed", f"{fear_value} — {fear_label}")

selected = st.selectbox(
    "Инструмент",
    list(SYMBOLS.keys()),
    format_func=lambda x: f"{SYMBOLS[x]} ({x})",
)

if st.button("Запустить анализ", type="primary"):
    try:
        df = indicators(fetch_yahoo(selected))
        action, price, stop, take, confidence, score, agents = analyze(df, fear_value)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Решение", action)
        c2.metric("Цена", f"{price:.4f}")
        c3.metric("Уверенность", f"{confidence}%")
        c4.metric("Баллы", score)

        if action != "WAIT":
            st.write(f"**Стоп:** {stop:.4f} | **Тейк:** {take:.4f}")

        st.dataframe(pd.DataFrame([
            {
                "Агент": name,
                "Решение": "BUY" if agent_score > 0 else "SELL" if agent_score < 0 else "WAIT",
                "Баллы": agent_score,
                "Причина": reason,
            }
            for name, agent_score, reason in agents
        ]), use_container_width=True)

        st.line_chart(df[["close"]].tail(300))
    except Exception as exc:
        st.error(f"Ошибка: {exc}")

st.info("Стабильная версия не записывает файлы и не вызывает цикл перезапуска.")
