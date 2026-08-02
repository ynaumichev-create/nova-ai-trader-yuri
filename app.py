
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

st.set_page_config(page_title="NOVA AI Trader", layout="wide")
st.title("NOVA AI Trader")
st.caption("Сигналы, демо-портфель и бэктест в одном приложении. Реальные деньги не подключены.")

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))

@st.cache_data(ttl=900)
def fetch_yahoo(symbol, data_range):
    encoded = urllib.parse.quote(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval={INTERVAL}&range={data_range}&includePrePost=false"
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

def analyze_row(df, i):
    row = df.iloc[i]
    prev20 = df.iloc[max(0, i-20):i+1]

    agents = {}
    agents["Trend"] = 2 if row["close"] > row["ema20"] > row["ema50"] > row["ema200"] else -2 if row["close"] < row["ema20"] < row["ema50"] < row["ema200"] else 1 if row["close"] > row["ema50"] else -1

    momentum = 1 if row["macd"] > row["macd_signal"] else -1
    momentum += 1 if 52 <= row["rsi"] <= 68 else -1 if 32 <= row["rsi"] <= 48 else 0
    agents["Momentum"] = momentum

    agents["Volume"] = 1 if row["obv"] > prev20["obv"].iloc[0] else -1
    agents["Price Action"] = 2 if row["close"] >= row["high20"] * 0.995 else -2 if row["close"] <= row["low20"] * 1.005 else 1 if row["close"] > row["ema20"] else -1

    atr_pct = row["atr"] / row["close"] * 100
    agents["Risk"] = -2 if atr_pct > 3.5 else -1 if atr_pct > 2 else 1

    score = sum(agents[k] for k in ("Trend", "Momentum", "Volume", "Price Action"))
    action = "WAIT" if agents["Risk"] <= -2 else "BUY" if score >= 4 else "SELL" if score <= -4 else "WAIT"

    price = float(row["close"])
    atr_value = float(row["atr"])
    stop = price - 1.5 * atr_value if action == "BUY" else price + 1.5 * atr_value if action == "SELL" else None
    take = price + 3.0 * atr_value if action == "BUY" else price - 3.0 * atr_value if action == "SELL" else None

    return {
        "action": action,
        "price": price,
        "stop": stop,
        "take": take,
        "score": score,
        "confidence": min(95, 50 + abs(score) * 8),
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

def backtest(symbol):
    df = indicators(fetch_yahoo(symbol, BACKTEST_RANGE))
    balance = START_BALANCE
    position = None
    trades = []
    equity = []
    peak = balance
    max_dd = 0.0

    for i in range(1, len(df)):
        row = df.iloc[i]
        t = df.index[i]
        result = analyze_row(df, i)
        price = float(row["close"])

        if position:
            if position["direction"] == "BUY":
                stop_hit = row["low"] <= position["stop"]
                take_hit = row["high"] >= position["take"]
                exit_price = position["stop"] if stop_hit else position["take"] if take_hit else None
                reverse = result["action"] == "SELL"
            else:
                stop_hit = row["high"] >= position["stop"]
                take_hit = row["low"] <= position["take"]
                exit_price = position["stop"] if stop_hit else position["take"] if take_hit else None
                reverse = result["action"] == "BUY"

            if reverse and exit_price is None:
                exit_price = price

            if exit_price is not None:
                pnl = (exit_price - position["entry"]) * position["qty"] if position["direction"] == "BUY" else (position["entry"] - exit_price) * position["qty"]
                balance += pnl
                trades.append({
                    "entry_time": position["entry_time"],
                    "exit_time": t,
                    "direction": position["direction"],
                    "entry": round(position["entry"], 4),
                    "exit": round(exit_price, 4),
                    "pnl": round(pnl, 4),
                    "balance": round(balance, 4),
                })
                position = None

        if position is None and result["action"] in ("BUY", "SELL"):
            risk_amount = balance * RISK_PER_TRADE
            distance = abs(result["price"] - result["stop"])
            qty = risk_amount / distance if distance else 0
            position = {
                "direction": result["action"],
                "entry": result["price"],
                "entry_time": t,
                "stop": result["stop"],
                "take": result["take"],
                "qty": qty,
            }

        floating = 0
        if position:
            floating = (price - position["entry"]) * position["qty"] if position["direction"] == "BUY" else (position["entry"] - price) * position["qty"]
        eq = balance + floating
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100 if peak else 0
        max_dd = max(max_dd, dd)
        equity.append({"time": t, "equity": eq})

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity).set_index("time")

    if trades_df.empty:
        return balance, 0.0, 0.0, 0.0, max_dd, trades_df, equity_df

    wins = trades_df[trades_df["pnl"] > 0]
    losses = trades_df[trades_df["pnl"] < 0]
    gross_profit = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    winrate = len(wins) / len(trades_df) * 100
    return balance, (balance / START_BALANCE - 1) * 100, winrate, pf, max_dd, trades_df, equity_df

tab1, tab2, tab3 = st.tabs(["Сигналы", "Демо-портфель", "Бэктест"])

with tab1:
    if st.button("Обновить сигналы", type="primary"):
        for symbol, name in SYMBOLS.items():
            try:
                df = indicators(fetch_yahoo(symbol, LIVE_RANGE))
                result = analyze_row(df, len(df) - 1)

                st.subheader(f"{name} ({symbol})")
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Сигнал", result["action"])
                c2.metric("Цена", f'{result["price"]:.4f}')
                c3.metric("Уверенность", f'{result["confidence"]}%')
                c4.metric("ATR риск", f'{result["atr_pct"]:.2f}%')

                if result["action"] != "WAIT":
                    st.write(f'**Стоп:** {result["stop"]:.4f} | **Тейк:** {result["take"]:.4f}')

                st.dataframe(pd.DataFrame([
                    {"Агент": k, "Баллы": v, "Решение": "BUY" if v > 0 else "SELL" if v < 0 else "WAIT"}
                    for k, v in result["agents"].items()
                ]), use_container_width=True)

                st.line_chart(df[["close"]].tail(300))
            except Exception as e:
                st.error(f"{symbol}: {e}")

with tab2:
    state = load_state()
    if st.button("Обновить демо-портфель"):
        for symbol in SYMBOLS:
            try:
                df = indicators(fetch_yahoo(symbol, LIVE_RANGE))
                result = analyze_row(df, len(df) - 1)
                update_paper(symbol, result, state)
            except Exception as e:
                st.error(f"{symbol}: {e}")
        st.success("Демо-портфель обновлен.")

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
        st.download_button("Скачать историю сделок", trades.to_csv(index=False).encode("utf-8"), "paper_trades.csv")

with tab3:
    selected = st.selectbox("Инструмент для проверки", list(SYMBOLS.keys()), format_func=lambda x: f"{SYMBOLS[x]} ({x})")
    if st.button("Запустить бэктест"):
        try:
            balance, ret, winrate, pf, max_dd, trades_df, equity_df = backtest(selected)

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Баланс", f"${balance:.2f}")
            c2.metric("Доходность", f"{ret:.2f}%")
            c3.metric("Win rate", f"{winrate:.1f}%")
            c4.metric("Profit factor", "∞" if pf == float("inf") else f"{pf:.2f}")
            c5.metric("Max drawdown", f"{max_dd:.2f}%")

            st.line_chart(equity_df)

            if not trades_df.empty:
                st.dataframe(trades_df.tail(300), use_container_width=True)
                st.download_button("Скачать бэктест CSV", trades_df.to_csv(index=False).encode("utf-8"), f"{selected}_backtest.csv")
        except Exception as e:
            st.error(str(e))
