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
st.title("NOVA AI Trader — Multi-Agent")
st.caption("5 независимых агентов + итоговый арбитр. Только демо.")

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

def ema_series(values, period):
    return pd.Series(values).ewm(span=period, adjust=False).mean()

def rsi_series(values, period=14):
    s = pd.Series(values)
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))

def atr_series(df, period=14):
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def macd(values):
    s = pd.Series(values)
    fast = s.ewm(span=12, adjust=False).mean()
    slow = s.ewm(span=26, adjust=False).mean()
    line = fast - slow
    signal = line.ewm(span=9, adjust=False).mean()
    return float(line.iloc[-1]), float(signal.iloc[-1])

def stochastic(df, period=14):
    low = df["low"].rolling(period).min()
    high = df["high"].rolling(period).max()
    k = 100 * (df["close"] - low) / (high - low)
    d = k.rolling(3).mean()
    return float(k.iloc[-1]), float(d.iloc[-1])

def obv(df):
    direction = df["close"].diff().apply(lambda x: 1 if x > 0 else -1 if x < 0 else 0)
    return (direction * df["volume"]).fillna(0).cumsum()

def vote_to_text(score):
    return "BUY" if score > 0 else "SELL" if score < 0 else "WAIT"

def analyze(symbol):
    candles = fetch_yahoo(symbol)
    df = pd.DataFrame(candles)
    closes = df["close"].tolist()

    price = closes[-1]
    ema20 = float(ema_series(closes, 20).iloc[-1])
    ema50 = float(ema_series(closes, 50).iloc[-1])
    ema200 = float(ema_series(closes, 200).iloc[-1])
    rsi14 = float(rsi_series(closes).iloc[-1])
    atr14 = float(atr_series(df).iloc[-1])
    macd_line, macd_signal = macd(closes)
    stoch_k, stoch_d = stochastic(df)
    obv_values = obv(df)
    volume_ratio = float(df["volume"].iloc[-1] / max(df["volume"].tail(20).mean(), 1))
    high20 = float(df["high"].tail(20).max())
    low20 = float(df["low"].tail(20).min())
    last_return = (price / closes[-2] - 1) * 100

    agents = []

    trend_score = 0
    trend_reason = []
    if price > ema20 > ema50 > ema200:
        trend_score = 2
        trend_reason.append("сильный восходящий тренд")
    elif price < ema20 < ema50 < ema200:
        trend_score = -2
        trend_reason.append("сильный нисходящий тренд")
    elif price > ema50:
        trend_score = 1
        trend_reason.append("цена выше EMA50")
    else:
        trend_score = -1
        trend_reason.append("цена ниже EMA50")
    agents.append(("Trend", trend_score, "; ".join(trend_reason)))

    momentum_score = 0
    momentum_reason = []
    if macd_line > macd_signal:
        momentum_score += 1
        momentum_reason.append("MACD вверх")
    else:
        momentum_score -= 1
        momentum_reason.append("MACD вниз")
    if 52 <= rsi14 <= 68:
        momentum_score += 1
        momentum_reason.append("RSI бычий")
    elif 32 <= rsi14 <= 48:
        momentum_score -= 1
        momentum_reason.append("RSI медвежий")
    if stoch_k > stoch_d and stoch_k < 80:
        momentum_score += 1
        momentum_reason.append("Stochastic вверх")
    elif stoch_k < stoch_d and stoch_k > 20:
        momentum_score -= 1
        momentum_reason.append("Stochastic вниз")
    agents.append(("Momentum", momentum_score, "; ".join(momentum_reason)))

    volume_score = 0
    volume_reason = []
    if obv_values.iloc[-1] > obv_values.iloc[-20]:
        volume_score += 1
        volume_reason.append("OBV растет")
    else:
        volume_score -= 1
        volume_reason.append("OBV снижается")
    if volume_ratio >= 1.25:
        volume_score += 1 if last_return > 0 else -1
        volume_reason.append("повышенный объем")
    agents.append(("Volume", volume_score, "; ".join(volume_reason)))

    price_score = 0
    price_reason = []
    if price >= high20 * 0.995:
        price_score += 2
        price_reason.append("тест пробоя 20-периодного максимума")
    elif price <= low20 * 1.005:
        price_score -= 2
        price_reason.append("тест пробоя 20-периодного минимума")
    elif price > ema20:
        price_score += 1
        price_reason.append("цена выше EMA20")
    else:
        price_score -= 1
        price_reason.append("цена ниже EMA20")
    agents.append(("Price Action", price_score, "; ".join(price_reason)))

    risk_score = 0
    risk_reason = []
    atr_pct = atr14 / price * 100
    if atr_pct > 3.5:
        risk_score = -2
        risk_reason.append("слишком высокая волатильность")
    elif atr_pct > 2:
        risk_score = -1
        risk_reason.append("повышенная волатильность")
    else:
        risk_score = 1
        risk_reason.append("волатильность приемлемая")
    agents.append(("Risk", risk_score, "; ".join(risk_reason)))

    raw_total = sum(a[1] for a in agents[:-1])
    risk_gate = agents[-1][1]
    if risk_gate <= -2:
        action = "WAIT"
    elif raw_total >= 4:
        action = "BUY"
    elif raw_total <= -4:
        action = "SELL"
    else:
        action = "WAIT"

    if action == "BUY":
        stop = price - 1.5 * atr14
        take = price + 3.0 * atr14
    elif action == "SELL":
        stop = price + 1.5 * atr14
        take = price - 3.0 * atr14
    else:
        stop = take = None

    position_size = 0
    if stop is not None:
        position_size = (ACCOUNT_SIZE_USDT * RISK_PER_TRADE) / abs(price - stop)

    agreement = abs(raw_total) / max(sum(abs(a[1]) for a in agents[:-1]), 1)
    confidence = round(min(95, 50 + agreement * 45))

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "name": SYMBOLS[symbol],
        "action": action,
        "price": round(price, 4),
        "stop": round(stop, 4) if stop else None,
        "take": round(take, 4) if take else None,
        "position_size": round(position_size, 6),
        "confidence": confidence,
        "raw_score": raw_total,
        "risk_score": risk_gate,
        "rsi14": round(rsi14, 2),
        "atr_pct": round(atr_pct, 2),
        "agents": agents,
        "candles": candles,
    }

def save_signal(result):
    row = {
        "timestamp": result["timestamp"],
        "symbol": result["symbol"],
        "action": result["action"],
        "price": result["price"],
        "stop": result["stop"],
        "take": result["take"],
        "position_size": result["position_size"],
        "confidence": result["confidence"],
        "raw_score": result["raw_score"],
        "risk_score": result["risk_score"],
    }
    exists = JOURNAL.exists()
    with JOURNAL.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            writer.writeheader()
        writer.writerow(row)

if st.button("Запустить совет ИИ", type="primary"):
    for symbol in SYMBOLS:
        try:
            result = analyze(symbol)
            save_signal(result)

            st.subheader(f'{result["name"]} ({result["symbol"]})')
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Итог", result["action"])
            c2.metric("Цена", result["price"])
            c3.metric("Уверенность", f'{result["confidence"]}%')
            c4.metric("Риск ATR", f'{result["atr_pct"]}%')

            if result["action"] != "WAIT":
                st.write(f'**Стоп:** {result["stop"]} | **Тейк:** {result["take"]} | **Размер позиции:** {result["position_size"]}')

            table = []
            for name, score, reason in result["agents"]:
                table.append({
                    "Агент": name,
                    "Решение": vote_to_text(score),
                    "Баллы": score,
                    "Причина": reason,
                })
            st.dataframe(pd.DataFrame(table), use_container_width=True)

            df = pd.DataFrame(result["candles"]).set_index("time")
            st.line_chart(df[["close"]])

        except Exception as e:
            st.error(f"{symbol}: {e}")

st.divider()
st.subheader("Журнал решений")
if JOURNAL.exists():
    st.dataframe(pd.read_csv(JOURNAL).tail(100), use_container_width=True)
else:
    st.info("Журнал появится после первого запуска.")
