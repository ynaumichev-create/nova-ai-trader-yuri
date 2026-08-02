
import json
import urllib.parse
import urllib.request

import pandas as pd
import streamlit as st

SYMBOLS = {
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
    "SOL-USD": "Solana",
    "XRP-USD": "XRP",
    "BNB-USD": "BNB",
    "ADA-USD": "Cardano",
    "DOGE-USD": "Dogecoin",
    "AVAX-USD": "Avalanche",
    "LINK-USD": "Chainlink",
    "DOT-USD": "Polkadot",
}

TIMEFRAMES = {
    "15m": ("15m", "1mo"),
    "1h": ("1h", "3mo"),
    "4h": ("1h", "6mo"),
    "1d": ("1d", "2y"),
}

st.set_page_config(page_title="NOVA AI Trader v0.3", layout="wide")
st.title("NOVA AI Trader v0.3")
st.caption("Сканер рынка, несколько таймфреймов и объяснение сигналов. Только демо.")

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))

@st.cache_data(ttl=900)
def fetch_yahoo(symbol, interval, data_range):
    encoded = urllib.parse.quote(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval={interval}&range={data_range}&includePrePost=false"
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
    if len(df) < 220:
        raise RuntimeError("Недостаточно данных")
    return df

@st.cache_data(ttl=3600)
def fear_greed():
    try:
        item = get_json("https://api.alternative.me/fng/?limit=1")["data"][0]
        return int(item["value"]), item["value_classification"]
    except Exception:
        return 50, "Neutral"

def resample_4h(df):
    return df.resample("4h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()

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

def analyze(symbol, timeframe, fear_value):
    interval, data_range = TIMEFRAMES[timeframe]
    df = fetch_yahoo(symbol, interval, data_range)
    if timeframe == "4h":
        df = resample_4h(df)
    df = indicators(df)

    row = df.iloc[-1]
    prev20 = df.tail(20)
    price = float(row["close"])
    reasons = []
    agents = []

    trend = 2 if price > row["ema20"] > row["ema50"] > row["ema200"] else -2 if price < row["ema20"] < row["ema50"] < row["ema200"] else 1 if price > row["ema50"] else -1
    reasons.append("тренд вверх" if trend > 0 else "тренд вниз")
    agents.append(("Trend", trend))

    momentum = 1 if row["macd"] > row["macd_signal"] else -1
    momentum += 1 if 52 <= row["rsi"] <= 68 else -1 if 32 <= row["rsi"] <= 48 else 0
    reasons.append(f"RSI {row['rsi']:.1f}")
    agents.append(("Momentum", momentum))

    volume = 1 if row["obv"] > prev20["obv"].iloc[0] else -1
    reasons.append("объем подтверждает движение" if volume > 0 else "объем не подтверждает рост")
    agents.append(("Volume", volume))

    price_action = 2 if price >= row["high20"] * 0.995 else -2 if price <= row["low20"] * 1.005 else 1 if price > row["ema20"] else -1
    agents.append(("Price Action", price_action))

    sentiment = 1 if fear_value >= 60 else -1 if fear_value <= 40 else 0
    agents.append(("Sentiment", sentiment))

    atr_pct = float(row["atr"] / price * 100)
    risk = -2 if atr_pct > 3.5 else -1 if atr_pct > 2 else 1
    agents.append(("Risk", risk))

    score = trend + momentum + volume + price_action + sentiment
    action = "WAIT" if risk <= -2 else "BUY" if score >= 5 else "SELL" if score <= -5 else "WAIT"

    atr_value = float(row["atr"])
    stop = price - 1.5 * atr_value if action == "BUY" else price + 1.5 * atr_value if action == "SELL" else None
    take = price + 3.0 * atr_value if action == "BUY" else price - 3.0 * atr_value if action == "SELL" else None

    nova_score = min(100, max(0, round(50 + score * 7 - max(0, atr_pct - 1.5) * 5)))
    confidence = min(95, max(50, round(50 + abs(score) * 7)))

    explanation = (
        f"{action}: {', '.join(reasons)}. "
        f"Волатильность ATR {atr_pct:.2f}%. "
        f"Итоговый балл {score}."
    )

    return {
        "Инструмент": SYMBOLS[symbol],
        "Тикер": symbol,
        "Таймфрейм": timeframe,
        "Сигнал": action,
        "NOVA Score": nova_score,
        "Уверенность": confidence,
        "Цена": round(price, 4),
        "RSI": round(float(row["rsi"]), 1),
        "ATR %": round(atr_pct, 2),
        "Стоп": round(stop, 4) if stop else None,
        "Тейк": round(take, 4) if take else None,
        "Объяснение": explanation,
        "_df": df,
        "_agents": agents,
    }

fear_value, fear_label = fear_greed()
st.metric("Fear & Greed", f"{fear_value} — {fear_label}")

tab1, tab2 = st.tabs(["Сканер рынка", "Разбор монеты"])

with tab1:
    timeframe = st.selectbox("Таймфрейм", list(TIMEFRAMES.keys()), index=1)
    if st.button("Сканировать рынок", type="primary"):
        rows = []
        progress = st.progress(0)
        for idx, symbol in enumerate(SYMBOLS, start=1):
            try:
                result = analyze(symbol, timeframe, fear_value)
                rows.append({k: v for k, v in result.items() if not k.startswith("_")})
            except Exception as exc:
                rows.append({
                    "Инструмент": SYMBOLS[symbol],
                    "Тикер": symbol,
                    "Таймфрейм": timeframe,
                    "Сигнал": "ERROR",
                    "NOVA Score": 0,
                    "Уверенность": 0,
                    "Цена": None,
                    "RSI": None,
                    "ATR %": None,
                    "Стоп": None,
                    "Тейк": None,
                    "Объяснение": str(exc),
                })
            progress.progress(idx / len(SYMBOLS))

        result_df = pd.DataFrame(rows).sort_values("NOVA Score", ascending=False)
        st.dataframe(result_df, use_container_width=True)
        st.download_button(
            "Скачать CSV",
            result_df.to_csv(index=False).encode("utf-8"),
            "nova_scan.csv",
            "text/csv",
        )

with tab2:
    symbol = st.selectbox("Монета", list(SYMBOLS.keys()), format_func=lambda x: f"{SYMBOLS[x]} ({x})")
    timeframe2 = st.selectbox("Таймфрейм анализа", list(TIMEFRAMES.keys()), index=1, key="tf2")

    if st.button("Разобрать монету"):
        try:
            result = analyze(symbol, timeframe2, fear_value)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Сигнал", result["Сигнал"])
            c2.metric("NOVA Score", result["NOVA Score"])
            c3.metric("Уверенность", f'{result["Уверенность"]}%')
            c4.metric("Цена", result["Цена"])

            st.write(result["Объяснение"])

            st.dataframe(pd.DataFrame([
                {
                    "Агент": name,
                    "Решение": "BUY" if score > 0 else "SELL" if score < 0 else "WAIT",
                    "Баллы": score,
                }
                for name, score in result["_agents"]
            ]), use_container_width=True)

            st.line_chart(result["_df"][["close"]].tail(300))
        except Exception as exc:
            st.error(str(exc))

st.info("v0.3 не открывает сделки и не использует платные API.")
