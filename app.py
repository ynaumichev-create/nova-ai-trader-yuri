# -*- coding: utf-8 -*-
import json
from pathlib import Path

import pandas as pd
import streamlit as st

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "portfolio.json"
TRADES_FILE = DATA_DIR / "trades.csv"
SIGNALS_FILE = DATA_DIR / "latest_signals.csv"
EQUITY_FILE = DATA_DIR / "equity.csv"

st.set_page_config(page_title="NOVA AI Trader v1.1", layout="wide")
st.title("NOVA AI Trader v1.1 — Autonomous Paper Trading")
st.caption("Автоматическая виртуальная торговля каждые 30 минут. Реальные деньги не подключены.")

state = (
    json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if STATE_FILE.exists()
    else {"balance": 1000.0, "positions": {}}
)

trades = pd.read_csv(TRADES_FILE) if TRADES_FILE.exists() else pd.DataFrame()
signals = pd.read_csv(SIGNALS_FILE) if SIGNALS_FILE.exists() else pd.DataFrame()
equity = pd.read_csv(EQUITY_FILE) if EQUITY_FILE.exists() else pd.DataFrame()

latest_equity = (
    float(equity.iloc[-1]["equity"])
    if not equity.empty
    else float(state["balance"])
)

realized_pnl = (
    float(trades.loc[trades["type"] == "CLOSE", "pnl"].sum())
    if not trades.empty
    else 0.0
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Капитал", f"${latest_equity:.2f}")
c2.metric("Свободный баланс", f"${state['balance']:.2f}")
c3.metric("Открытых позиций", len(state["positions"]))
c4.metric("Реализованный PnL", f"${realized_pnl:.2f}")

st.subheader("Открытые позиции")
if state["positions"]:
    latest_prices = (
        dict(zip(signals["symbol"], signals["price"]))
        if not signals.empty
        else {}
    )

    rows = []
    for symbol, position in state["positions"].items():
        price = float(latest_prices.get(symbol, position["entry"]))

        if position["side"] == "BUY":
            pnl = (price - position["entry"]) * position["qty"]
        else:
            pnl = (position["entry"] - price) * position["qty"]

        rows.append({
            "Монета": position["name"],
            "Тикер": symbol,
            "Сторона": position["side"],
            "Вход": round(position["entry"], 8),
            "Текущая": round(price, 8),
            "Стоп": round(position["stop"], 8),
            "Тейк": round(position["take"], 8),
            "NOVA Score": position["score"],
            "PnL": round(pnl, 4),
            "Открыта": position["opened_at"],
        })

    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
else:
    st.info("Открытых позиций пока нет.")

st.subheader("Статистика")
if not trades.empty:
    closed = trades[trades["type"] == "CLOSE"]

    if not closed.empty:
        wins = closed[closed["pnl"] > 0]
        losses = closed[closed["pnl"] < 0]

        win_rate = len(wins) / len(closed) * 100
        gross_profit = wins["pnl"].sum()
        gross_loss = abs(losses["pnl"].sum())
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        avg_win = wins["pnl"].mean() if not wins.empty else 0.0
        avg_loss = losses["pnl"].mean() if not losses.empty else 0.0

        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric("Закрыто сделок", len(closed))
        s2.metric("Win rate", f"{win_rate:.1f}%")
        s3.metric(
            "Profit Factor",
            "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}",
        )
        s4.metric("Средняя прибыль", f"${avg_win:.2f}")
        s5.metric("Средний убыток", f"${avg_loss:.2f}")
    else:
        st.info("Закрытых сделок пока нет.")
else:
    st.info("Торговая история появится после первого автоматического цикла.")

st.subheader("Кривая капитала")
if not equity.empty:
    equity["timestamp"] = pd.to_datetime(equity["timestamp"])
    st.line_chart(equity.set_index("timestamp")[["equity"]])
else:
    st.info("Кривая появится после первого автоматического цикла.")

st.subheader("История сделок")
if not trades.empty:
    st.dataframe(trades.tail(300), use_container_width=True, hide_index=True)
    st.download_button(
        "Скачать историю CSV",
        trades.to_csv(index=False).encode("utf-8-sig"),
        "nova_trades.csv",
        "text/csv",
    )
else:
    st.info("Сделок пока нет.")

st.subheader("Последние сильные сигналы")
if not signals.empty:
    strong = signals[
        signals["signal"].isin(["BUY", "SELL"])
    ].sort_values("nova_score", ascending=False)

    if strong.empty:
        st.info("Сильных сигналов сейчас нет.")
    else:
        st.dataframe(strong.head(30), use_container_width=True, hide_index=True)
else:
    st.info("Сигналы появятся после первого запуска GitHub Actions.")

st.success("Автоматический цикл настроен на запуск каждые 30 минут через GitHub Actions.")
