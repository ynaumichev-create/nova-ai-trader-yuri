
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

SYMBOLS = {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana"}
INTERVAL = "1h"
RANGE = "3mo"

st.set_page_config(page_title="NOVA AI Trader Free", layout="wide")
st.title("NOVA AI Trader — Free AI Council")
st.caption("Полностью бесплатная версия: 5 независимых агентов и итоговый арбитр.")

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

def agent_decisions(df):
    row = df.iloc[-1]
    prev20 = df.tail(20)
    price = float(row["close"])

    trend_score = 2 if price > row["ema20"] > row["ema50"] > row["ema200"] else -2 if price < row["ema20"] < row["ema50"] < row["ema200"] else 1 if price > row["ema50"] else -1
    trend_reason = "EMA подтверждают направление"

    momentum_score = (1 if row["macd"] > row["macd_signal"] else -1)
    momentum_score += 1 if 52 <= row["rsi"] <= 68 else -1 if 32 <= row["rsi"] <= 48 else 0
    momentum_reason = f"RSI {row['rsi']:.1f}, MACD {'выше' if row['macd'] > row['macd_signal'] else 'ниже'} сигнальной"

    volume_score = 1 if row["obv"] > prev20["obv"].iloc[0] else -1
    volume_reason = "OBV растет" if volume_score > 0 else "OBV снижается"

    price_score = 2 if price >= row["high20"] * 0.995 else -2 if price <= row["low20"] * 1.005 else 1 if price > row["ema20"] else -1
    price_reason = "пробой/положение относительно EMA20"

    atr_pct = float(row["atr"] / price * 100)
    risk_score = -2 if atr_pct > 3.5 else -1 if atr_pct > 2 else 1
    risk_reason = f"ATR {atr_pct:.2f}%"

    return [
        {"agent": "Trend Agent", "score": trend_score, "reason": trend_reason},
        {"agent": "Momentum Agent", "score": momentum_score, "reason": momentum_reason},
        {"agent": "Volume Agent", "score": volume_score, "reason": volume_reason},
        {"agent": "Price Action Agent", "score": price_score, "reason": price_reason},
        {"agent": "Risk Agent", "score": risk_score, "reason": risk_reason},
    ], price, float(row["atr"])

def decision_text(score):
    return "BUY" if score > 0 else "SELL" if score < 0 else "WAIT"

def arbitrate(agents):
    market_score = sum(a["score"] for a in agents[:-1])
    risk_score = agents[-1]["score"]

    if risk_score <= -2:
        action = "WAIT"
    elif market_score >= 4:
        action = "BUY"
    elif market_score <= -4:
        action = "SELL"
    else:
        action = "WAIT"

    agreement = abs(market_score) / max(sum(abs(a["score"]) for a in agents[:-1]), 1)
    confidence = round(min(95, 50 + agreement * 45))
    return action, market_score, confidence

selected = st.selectbox("Инструмент", list(SYMBOLS.keys()), format_func=lambda x: f"{SYMBOLS[x]} ({x})")

if st.button("Запустить бесплатный совет ИИ", type="primary"):
    try:
        df = indicators(fetch_yahoo(selected))
        agents, price, atr_value = agent_decisions(df)
        action, total_score, confidence = arbitrate(agents)

        stop = take = None
        if action == "BUY":
            stop = price - 1.5 * atr_value
            take = price + 3.0 * atr_value
        elif action == "SELL":
            stop = price + 1.5 * atr_value
            take = price - 3.0 * atr_value

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Решение NOVA", action)
        c2.metric("Цена", f"{price:.4f}")
        c3.metric("Уверенность", f"{confidence}%")
        c4.metric("Общий балл", total_score)

        if action != "WAIT":
            st.write(f"**Стоп:** {stop:.4f} | **Тейк:** {take:.4f}")

        table = []
        for a in agents:
            table.append({
                "Агент": a["agent"],
                "Решение": decision_text(a["score"]),
                "Баллы": a["score"],
                "Причина": a["reason"],
            })
        st.dataframe(pd.DataFrame(table), use_container_width=True)

        st.line_chart(df[["close"]].tail(300))

        st.info("Это полностью бесплатная тестовая версия. Сделки автоматически не открываются.")
    except Exception as e:
        st.error(str(e))
