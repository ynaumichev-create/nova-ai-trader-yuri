
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
LIVE_RANGE = "3mo"
BACKTEST_RANGE = "2y"
START_BALANCE = 1000.0
RISK_PER_TRADE = 0.01

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE = DATA_DIR / "portfolio.json"
TRADES = DATA_DIR / "paper_trades.csv"

st.set_page_config(page_title="NOVA AI Trader Free", layout="wide")
st.title("NOVA AI Trader — Free Complete")
st.caption("6 бесплатных агентов, демо-портфель, бэктест и Telegram.")

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))

@st.cache_data(ttl=900)
def fetch_yahoo(symbol, data_range):
    encoded = urllib.parse.quote(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval={INTERVAL}&range={data_range}&includePrePost=false"
    result = get_json(url)["chart"]["result"][0]
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        vals = [q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]]
        if any(v is None for v in vals):
            continue
        rows.append({
            "time": pd.to_datetime(t, unit="s", utc=True),
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
        data = get_json("https://api.alternative.me/fng/?limit=1")
        item = data["data"][0]
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
        (x["low"] - prev_close).abs()
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
    agents.append(("Volume", volume, "Изменение OBV"))

    price_action = 2 if price >= row["high20"] * 0.995 else -2 if price <= row["low20"] * 1.005 else 1 if price > row["ema20"] else -1
    agents.append(("Price Action", price_action, "Уровни и EMA20"))

    atr_pct = float(row["atr"] / price * 100)
    risk = -2 if atr_pct > 3.5 else -1 if atr_pct > 2 else 1
    agents.append(("Risk", risk, f"ATR {atr_pct:.2f}%"))

    sentiment = 1 if fear_value >= 60 else -1 if fear_value <= 40 else 0
    agents.append(("Sentiment", sentiment, f"Fear & Greed {fear_value}"))

    market_score = trend + momentum + volume + price_action + sentiment
    if risk <= -2:
        action = "WAIT"
    elif market_score >= 5:
        action = "BUY"
    elif market_score <= -5:
        action = "SELL"
    else:
        action = "WAIT"

    atr_value = float(row["atr"])
    stop = price - 1.5 * atr_value if action == "BUY" else price + 1.5 * atr_value if action == "SELL" else None
    take = price + 3.0 * atr_value if action == "BUY" else price - 3.0 * atr_value if action == "SELL" else None
    agreement = abs(market_score) / max(sum(abs(a[1]) for a in agents if a[0] != "Risk"), 1)
    confidence = round(min(95, 50 + agreement * 45))

    return {
        "action": action,
        "price": price,
        "stop": stop,
        "take": take,
        "score": market_score,
        "confidence": confidence,
        "atr_pct": atr_pct,
        "agents": agents,
    }

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"balance": START_BALANCE, "positions": {}}

def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def append_trade(row):
    exists = TRADES.exists()
    with TRADES.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            writer.writeheader()
        writer.writerow(row)

def update_paper(symbol, result, state):
    position = state["positions"].get(symbol)
    price = result["price"]

    if position:
        direction = position["direction"]
        hit_stop = price <= position["stop"] if direction == "BUY" else price >= position["stop"]
        hit_take = price >= position["take"] if direction == "BUY" else price <= position["take"]
        reverse = result["action"] not in ("WAIT", direction)

        if hit_stop or hit_take or reverse:
            pnl = (price - position["entry"]) * position["qty"] if direction == "BUY" else (position["entry"] - price) * position["qty"]
            state["balance"] += pnl
            append_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "type": "CLOSE",
                "direction": direction,
                "price": round(price, 4),
                "qty": round(position["qty"], 6),
                "pnl": round(pnl, 4),
                "balance": round(state["balance"], 4),
                "reason": "take" if hit_take else "stop" if hit_stop else "reverse",
            })
            del state["positions"][symbol]
            position = None

    if not position and result["action"] in ("BUY", "SELL"):
        risk_amount = state["balance"] * RISK_PER_TRADE
        distance = abs(result["price"] - result["stop"])
        qty = risk_amount / distance if distance else 0
        state["positions"][symbol] = {
            "direction": result["action"],
            "entry": result["price"],
            "stop": result["stop"],
            "take": result["take"],
            "qty": qty,
        }
        append_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "type": "OPEN",
            "direction": result["action"],
            "price": round(result["price"], 4),
            "qty": round(qty, 6),
            "pnl": 0,
            "balance": round(state["balance"], 4),
            "reason": "signal",
        })
    save_state(state)

def send_telegram(text):
    try:
        token = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return False
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=20):
            return True
    except Exception:
        return False

fear_value, fear_label = fetch_fear_greed()
st.metric("Fear & Greed", f"{fear_value} — {fear_label}")

tab1, tab2 = st.tabs(["Совет агентов", "Демо-портфель"])

with tab1:
    selected = st.selectbox("Инструмент", list(SYMBOLS.keys()), format_func=lambda x: f"{SYMBOLS[x]} ({x})")
    if st.button("Запустить бесплатный совет", type="primary"):
        try:
            df = indicators(fetch_yahoo(selected, LIVE_RANGE))
            result = analyze(df, fear_value)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Решение", result["action"])
            c2.metric("Цена", f'{result["price"]:.4f}')
            c3.metric("Уверенность", f'{result["confidence"]}%')
            c4.metric("Баллы", result["score"])

            if result["action"] != "WAIT":
                st.write(f'**Стоп:** {result["stop"]:.4f} | **Тейк:** {result["take"]:.4f}')

            table = [{"Агент": n, "Решение": "BUY" if s > 0 else "SELL" if s < 0 else "WAIT", "Баллы": s, "Причина": r} for n, s, r in result["agents"]]
            st.dataframe(pd.DataFrame(table), use_container_width=True)
            st.line_chart(df[["close"]].tail(300))

            msg = f"{selected}: {result['action']}\nЦена: {result['price']:.4f}\nУверенность: {result['confidence']}%"
            if send_telegram(msg):
                st.success("Сигнал отправлен в Telegram.")
        except Exception as e:
            st.error(str(e))

with tab2:
    state = load_state()
    if st.button("Обновить демо-портфель"):
        for symbol in SYMBOLS:
            try:
                df = indicators(fetch_yahoo(symbol, LIVE_RANGE))
                result = analyze(df, fear_value)
                update_paper(symbol, result, state)
            except Exception as e:
                st.error(f"{symbol}: {e}")
        st.success("Демо-портфель обновлён.")

    state = load_state()
    c1, c2 = st.columns(2)
    c1.metric("Баланс", f"${state['balance']:.2f}")
    c2.metric("Открытых позиций", len(state["positions"]))

    if state["positions"]:
        st.dataframe(pd.DataFrame([
            {
                "Инструмент": symbol,
                "Направление": p["direction"],
                "Вход": round(p["entry"], 4),
                "Стоп": round(p["stop"], 4),
                "Тейк": round(p["take"], 4),
                "Количество": round(p["qty"], 6),
            }
            for symbol, p in state["positions"].items()
        ]), use_container_width=True)

    if TRADES.exists():
        trades = pd.read_csv(TRADES)
        st.dataframe(trades.tail(100), use_container_width=True)
        st.download_button("Скачать историю", trades.to_csv(index=False).encode("utf-8"), "paper_trades.csv")
