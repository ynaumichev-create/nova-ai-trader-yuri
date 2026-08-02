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
RANGE = "3mo"
START_BALANCE = 1000.0
RISK_PER_TRADE = 0.01

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
TRADES = DATA_DIR / "paper_trades.csv"
STATE = DATA_DIR / "portfolio.json"
WEIGHTS = DATA_DIR / "agent_weights.json"

DEFAULT_WEIGHTS = {
    "Trend": 1.0,
    "Momentum": 1.0,
    "Volume": 1.0,
    "Price Action": 1.0,
    "Risk": 1.0,
}

st.set_page_config(page_title="NOVA AI Trader", layout="wide")
st.title("NOVA AI Trader — Learning Engine")
st.caption("Демо-торговля + адаптивные веса агентов. Реальные деньги не подключены.")

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
    return pd.Series(values).ewm(span=period, adjust=False).mean()

def rsi(values, period=14):
    s = pd.Series(values)
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))

def atr(df, period=14):
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def macd(values):
    s = pd.Series(values)
    line = s.ewm(span=12, adjust=False).mean() - s.ewm(span=26, adjust=False).mean()
    signal = line.ewm(span=9, adjust=False).mean()
    return line, signal

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"balance": START_BALANCE, "positions": {}}

def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def load_weights():
    if WEIGHTS.exists():
        return json.loads(WEIGHTS.read_text(encoding="utf-8"))
    return DEFAULT_WEIGHTS.copy()

def save_weights(weights):
    WEIGHTS.write_text(json.dumps(weights, ensure_ascii=False, indent=2), encoding="utf-8")

def analyze(symbol, weights):
    candles = fetch_yahoo(symbol)
    df = pd.DataFrame(candles)
    closes = df["close"]
    price = float(closes.iloc[-1])

    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    r = rsi(closes)
    a = atr(df)
    m, ms = macd(closes)
    obv = (df["close"].diff().apply(lambda x: 1 if x > 0 else -1 if x < 0 else 0) * df["volume"]).fillna(0).cumsum()

    agents = {}

    trend = 2 if price > e20.iloc[-1] > e50.iloc[-1] > e200.iloc[-1] else -2 if price < e20.iloc[-1] < e50.iloc[-1] < e200.iloc[-1] else 1 if price > e50.iloc[-1] else -1
    agents["Trend"] = trend

    momentum = 1 if m.iloc[-1] > ms.iloc[-1] else -1
    momentum += 1 if 52 <= r.iloc[-1] <= 68 else -1 if 32 <= r.iloc[-1] <= 48 else 0
    agents["Momentum"] = momentum

    agents["Volume"] = 1 if obv.iloc[-1] > obv.iloc[-20] else -1

    high20 = float(df["high"].tail(20).max())
    low20 = float(df["low"].tail(20).min())
    agents["Price Action"] = 2 if price >= high20 * 0.995 else -2 if price <= low20 * 1.005 else 1 if price > e20.iloc[-1] else -1

    atr_pct = float(a.iloc[-1] / price * 100)
    agents["Risk"] = -2 if atr_pct > 3.5 else -1 if atr_pct > 2 else 1

    weighted_score = sum(agents[name] * weights.get(name, 1.0) for name in ("Trend", "Momentum", "Volume", "Price Action"))
    risk_gate = agents["Risk"]

    action = "WAIT" if risk_gate <= -2 else "BUY" if weighted_score >= 4 else "SELL" if weighted_score <= -4 else "WAIT"

    atr_value = float(a.iloc[-1])
    stop = price - 1.5 * atr_value if action == "BUY" else price + 1.5 * atr_value if action == "SELL" else None
    take = price + 3 * atr_value if action == "BUY" else price - 3 * atr_value if action == "SELL" else None

    confidence = min(95, round(50 + min(abs(weighted_score), 6) * 7))

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "name": SYMBOLS[symbol],
        "action": action,
        "price": round(price, 4),
        "stop": round(stop, 4) if stop else None,
        "take": round(take, 4) if take else None,
        "confidence": confidence,
        "weighted_score": round(weighted_score, 2),
        "atr_pct": round(atr_pct, 2),
        "agents": agents,
        "candles": candles,
    }

def append_trade(row):
    exists = TRADES.exists()
    with TRADES.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            writer.writeheader()
        writer.writerow(row)

def learn_from_closed_trade(position, pnl, weights):
    direction_sign = 1 if position["direction"] == "BUY" else -1
    success = 1 if pnl > 0 else -1

    for name, raw_score in position["agent_scores"].items():
        if name == "Risk":
            continue
        aligned = 1 if raw_score * direction_sign > 0 else -1
        delta = 0.05 * success * aligned
        weights[name] = round(min(2.0, max(0.5, weights.get(name, 1.0) + delta)), 3)

    save_weights(weights)

def process_trade(result, state, weights):
    symbol = result["symbol"]
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
            learn_from_closed_trade(position, pnl, weights)

            append_trade({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "type": "CLOSE",
                "direction": direction,
                "price": price,
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
            "agent_scores": result["agents"],
        }

        append_trade({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "type": "OPEN",
            "direction": result["action"],
            "price": result["price"],
            "qty": round(qty, 6),
            "pnl": 0,
            "balance": round(state["balance"], 4),
            "reason": "signal",
        })

    save_state(state)

state = load_state()
weights = load_weights()

if st.button("Запустить анализ и обучение", type="primary"):
    for symbol in SYMBOLS:
        try:
            result = analyze(symbol, weights)
            process_trade(result, state, weights)

            st.subheader(f'{result["name"]} ({symbol})')
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Сигнал", result["action"])
            c2.metric("Цена", result["price"])
            c3.metric("Уверенность", f'{result["confidence"]}%')
            c4.metric("Взвешенный балл", result["weighted_score"])

            table = []
            for name, score in result["agents"].items():
                table.append({
                    "Агент": name,
                    "Сигнал": "BUY" if score > 0 else "SELL" if score < 0 else "WAIT",
                    "Баллы": score,
                    "Вес": weights.get(name, 1.0),
                    "Вклад": round(score * weights.get(name, 1.0), 2),
                })
            st.dataframe(pd.DataFrame(table), use_container_width=True)

            df = pd.DataFrame(result["candles"]).set_index("time")
            st.line_chart(df[["close"]])

        except Exception as e:
            st.error(f"{symbol}: {e}")

st.divider()
st.subheader("Обученные веса агентов")
st.dataframe(pd.DataFrame([
    {"Агент": name, "Вес": value}
    for name, value in weights.items()
]), use_container_width=True)

st.subheader("Демо-портфель")
state = load_state()
c1, c2 = st.columns(2)
c1.metric("Баланс", f"${state['balance']:.2f}")
c2.metric("Открытых позиций", len(state["positions"]))

if state["positions"]:
    st.dataframe(pd.DataFrame([
        {
            "Инструмент": symbol,
            "Направление": p["direction"],
            "Вход": p["entry"],
            "Стоп": p["stop"],
            "Тейк": p["take"],
            "Количество": round(p["qty"], 6),
        }
        for symbol, p in state["positions"].items()
    ]), use_container_width=True)

st.subheader("Статистика")
if TRADES.exists():
    trades = pd.read_csv(TRADES)
    closed = trades[trades["type"] == "CLOSE"].copy()

    if not closed.empty:
        wins = closed[closed["pnl"] > 0]
        losses = closed[closed["pnl"] < 0]
        winrate = len(wins) / len(closed) * 100
        gross_profit = wins["pnl"].sum()
        gross_loss = abs(losses["pnl"].sum())
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Закрыто сделок", len(closed))
        c2.metric("Win rate", f"{winrate:.1f}%")
        c3.metric("Profit factor", "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}")
        c4.metric("PnL", f"${closed['pnl'].sum():.2f}")

        equity = closed[["timestamp", "balance"]].copy()
        equity["timestamp"] = pd.to_datetime(equity["timestamp"])
        st.line_chart(equity.set_index("timestamp")[["balance"]])

    st.dataframe(trades.tail(100), use_container_width=True)
else:
    st.info("Статистика появится после первой закрытой сделки.")

st.divider()
c1, c2 = st.columns(2)
with c1:
    if STATE.exists():
        st.download_button("Скачать состояние портфеля", STATE.read_bytes(), "portfolio.json")
with c2:
    if TRADES.exists():
        st.download_button("Скачать историю сделок", TRADES.read_bytes(), "paper_trades.csv")
