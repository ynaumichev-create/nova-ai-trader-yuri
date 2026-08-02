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
INTERVAL = "1h"
RANGE = "3mo"

st.set_page_config(page_title="NOVA Market Scanner", layout="wide")
st.title("NOVA AI Trader — Market Scanner")
st.caption("Бесплатный сканер 10 криптовалют. Реальные сделки не открываются.")

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
def fear_greed():
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

def score_symbol(symbol, fear_value):
    df = indicators(fetch_yahoo(symbol))
    row = df.iloc[-1]
    prev20 = df.tail(20)
    price = float(row["close"])

    trend = 2 if price > row["ema20"] > row["ema50"] > row["ema200"] else -2 if price < row["ema20"] < row["ema50"] < row["ema200"] else 1 if price > row["ema50"] else -1
    momentum = 1 if row["macd"] > row["macd_signal"] else -1
    momentum += 1 if 52 <= row["rsi"] <= 68 else -1 if 32 <= row["rsi"] <= 48 else 0
    volume = 1 if row["obv"] > prev20["obv"].iloc[0] else -1
    price_action = 2 if price >= row["high20"] * 0.995 else -2 if price <= row["low20"] * 1.005 else 1 if price > row["ema20"] else -1
    atr_pct = float(row["atr"] / price * 100)
    risk = -2 if atr_pct > 3.5 else -1 if atr_pct > 2 else 1
    sentiment = 1 if fear_value >= 60 else -1 if fear_value <= 40 else 0

    total = trend + momentum + volume + price_action + sentiment
    action = "WAIT" if risk <= -2 else "BUY" if total >= 5 else "SELL" if total <= -5 else "WAIT"

    atr_value = float(row["atr"])
    stop = price - 1.5 * atr_value if action == "BUY" else price + 1.5 * atr_value if action == "SELL" else None
    take = price + 3.0 * atr_value if action == "BUY" else price - 3.0 * atr_value if action == "SELL" else None

    nova_score = min(100, max(0, round(50 + total * 7 - max(0, atr_pct - 1.5) * 5)))
    return {
        "Инструмент": SYMBOLS[symbol],
        "Тикер": symbol,
        "Сигнал": action,
        "NOVA Score": nova_score,
        "Цена": round(price, 4),
        "RSI": round(float(row["rsi"]), 1),
        "ATR %": round(atr_pct, 2),
        "Стоп": round(stop, 4) if stop else None,
        "Тейк": round(take, 4) if take else None,
    }

fear_value, fear_label = fear_greed()
st.metric("Fear & Greed", f"{fear_value} — {fear_label}")

if st.button("Сканировать рынок", type="primary"):
    results = []
    progress = st.progress(0)
    status = st.empty()

    for idx, symbol in enumerate(SYMBOLS, start=1):
        status.write(f"Проверяю {SYMBOLS[symbol]}...")
        try:
            results.append(score_symbol(symbol, fear_value))
        except Exception as exc:
            results.append({
                "Инструмент": SYMBOLS[symbol],
                "Тикер": symbol,
                "Сигнал": "ERROR",
                "NOVA Score": 0,
                "Цена": None,
                "RSI": None,
                "ATR %": None,
                "Стоп": None,
                "Тейк": str(exc),
            })
        progress.progress(idx / len(SYMBOLS))

    status.empty()
    result_df = pd.DataFrame(results).sort_values("NOVA Score", ascending=False)
    st.subheader("Лучшие возможности")
    st.dataframe(result_df, use_container_width=True)

    active = result_df[result_df["Сигнал"].isin(["BUY", "SELL"])]
    if active.empty:
        st.info("Сильных сигналов сейчас нет.")
    else:
        st.success(f"Найдено сильных сигналов: {len(active)}")

    st.download_button(
        "Скачать результаты CSV",
        result_df.to_csv(index=False).encode("utf-8"),
        "nova_market_scan.csv",
        "text/csv",
    )

st.info("Сканер использует бесплатные рыночные данные и не отправляет торговые ордера.")
