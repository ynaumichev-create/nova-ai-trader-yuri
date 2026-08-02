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
RANGE = "2y"
START_BALANCE = 1000.0
RISK_PER_TRADE = 0.01

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
TRADES = DATA_DIR / "backtest_trades.csv"

st.set_page_config(page_title="NOVA AI Trader", layout="wide")
st.title("NOVA AI Trader — Backtest")
st.caption("Проверка стратегии на истории. Реальные деньги не подключены.")

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))

@st.cache_data(ttl=3600)
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
    if len(df) < 300:
        raise RuntimeError("Недостаточно данных")
    return df

def add_indicators(df):
    x = df.copy()
    x["ema20"] = x["close"].ewm(span=20, adjust=False).mean()
    x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()
    x["ema200"] = x["close"].ewm(span=200, adjust=False).mean()

    delta = x["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    x["rsi"] = 100 - (100 / (1 + rs))

    macd = x["close"].ewm(span=12, adjust=False).mean() - x["close"].ewm(span=26, adjust=False).mean()
    x["macd"] = macd
    x["macd_signal"] = macd.ewm(span=9, adjust=False).mean()

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

def signal_for_row(df, i):
    row = df.iloc[i]
    prev20 = df.iloc[max(0, i-20):i+1]

    trend = 2 if row["close"] > row["ema20"] > row["ema50"] > row["ema200"] else -2 if row["close"] < row["ema20"] < row["ema50"] < row["ema200"] else 1 if row["close"] > row["ema50"] else -1

    momentum = 1 if row["macd"] > row["macd_signal"] else -1
    momentum += 1 if 52 <= row["rsi"] <= 68 else -1 if 32 <= row["rsi"] <= 48 else 0

    volume = 1 if row["obv"] > prev20["obv"].iloc[0] else -1

    price_action = 2 if row["close"] >= row["high20"] * 0.995 else -2 if row["close"] <= row["low20"] * 1.005 else 1 if row["close"] > row["ema20"] else -1

    atr_pct = row["atr"] / row["close"] * 100
    risk = -2 if atr_pct > 3.5 else -1 if atr_pct > 2 else 1

    score = trend + momentum + volume + price_action

    if risk <= -2:
        return "WAIT", score
    if score >= 4:
        return "BUY", score
    if score <= -4:
        return "SELL", score
    return "WAIT", score

def run_backtest(symbol):
    df = add_indicators(fetch_yahoo(symbol))
    balance = START_BALANCE
    equity_points = []
    trades = []
    position = None
    peak = balance
    max_drawdown = 0.0

    for i in range(1, len(df)):
        row = df.iloc[i]
        timestamp = df.index[i]
        price = float(row["close"])
        action, score = signal_for_row(df, i)

        if position:
            if position["direction"] == "BUY":
                stop_hit = row["low"] <= position["stop"]
                take_hit = row["high"] >= position["take"]
                exit_price = position["stop"] if stop_hit else position["take"] if take_hit else None
                reverse = action == "SELL"
            else:
                stop_hit = row["high"] >= position["stop"]
                take_hit = row["low"] <= position["take"]
                exit_price = position["stop"] if stop_hit else position["take"] if take_hit else None
                reverse = action == "BUY"

            if reverse and exit_price is None:
                exit_price = price

            if exit_price is not None:
                pnl = (exit_price - position["entry"]) * position["qty"] if position["direction"] == "BUY" else (position["entry"] - exit_price) * position["qty"]
                balance += pnl
                trades.append({
                    "symbol": symbol,
                    "entry_time": position["entry_time"],
                    "exit_time": timestamp,
                    "direction": position["direction"],
                    "entry": round(position["entry"], 4),
                    "exit": round(exit_price, 4),
                    "qty": round(position["qty"], 6),
                    "pnl": round(pnl, 4),
                    "balance": round(balance, 4),
                    "reason": "stop" if stop_hit else "take" if take_hit else "reverse",
                })
                position = None

        if position is None and action in ("BUY", "SELL"):
            atr_value = float(row["atr"])
            stop = price - 1.5 * atr_value if action == "BUY" else price + 1.5 * atr_value
            take = price + 3.0 * atr_value if action == "BUY" else price - 3.0 * atr_value
            risk_amount = balance * RISK_PER_TRADE
            distance = abs(price - stop)
            qty = risk_amount / distance if distance > 0 else 0
            position = {
                "direction": action,
                "entry": price,
                "entry_time": timestamp,
                "stop": stop,
                "take": take,
                "qty": qty,
            }

        floating = 0.0
        if position:
            floating = (price - position["entry"]) * position["qty"] if position["direction"] == "BUY" else (position["entry"] - price) * position["qty"]

        equity = balance + floating
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak * 100 if peak else 0
        max_drawdown = max(max_drawdown, drawdown)

        equity_points.append({"time": timestamp, "equity": equity})

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity_points).set_index("time")

    if trades_df.empty:
        return {
            "symbol": symbol,
            "trades": trades_df,
            "equity": equity_df,
            "final_balance": balance,
            "return_pct": 0.0,
            "winrate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": max_drawdown,
        }

    wins = trades_df[trades_df["pnl"] > 0]
    losses = trades_df[trades_df["pnl"] < 0]
    gross_profit = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "symbol": symbol,
        "trades": trades_df,
        "equity": equity_df,
        "final_balance": balance,
        "return_pct": (balance / START_BALANCE - 1) * 100,
        "winrate": len(wins) / len(trades_df) * 100,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown,
    }

selected = st.selectbox("Инструмент", list(SYMBOLS.keys()), format_func=lambda x: f"{SYMBOLS[x]} ({x})")

if st.button("Запустить бэктест", type="primary"):
    try:
        result = run_backtest(selected)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Итоговый баланс", f"${result['final_balance']:.2f}")
        c2.metric("Доходность", f"{result['return_pct']:.2f}%")
        c3.metric("Win rate", f"{result['winrate']:.1f}%")
        c4.metric("Profit factor", "∞" if result["profit_factor"] == float("inf") else f"{result['profit_factor']:.2f}")
        c5.metric("Max drawdown", f"{result['max_drawdown']:.2f}%")

        st.subheader("Кривая капитала")
        st.line_chart(result["equity"])

        st.subheader("Сделки")
        if result["trades"].empty:
            st.info("Сделок не найдено.")
        else:
            st.dataframe(result["trades"].tail(300), use_container_width=True)
            csv_data = result["trades"].to_csv(index=False).encode("utf-8")
            st.download_button("Скачать сделки CSV", csv_data, f"{selected}_backtest.csv", "text/csv")

    except Exception as e:
        st.error(str(e))
