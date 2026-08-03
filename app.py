
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

COINS_COUNT = 150
START_BALANCE = 1000.0
MAX_POSITIONS = 5
POSITION_SHARE = 0.10
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06

st.set_page_config(page_title="NOVA AI Trader v1.0", layout="wide")
st.title("NOVA AI Trader v1.0 — Paper Trading")
st.caption("Сканер 150 монет + автоматические виртуальные сделки. Реальные деньги не подключены.")

def get_json(url, headers=None, retries=3):
    request_headers = {
        "User-Agent": "Mozilla/5.0 NOVA-AI-Trader",
        "Accept": "application/json",
    }
    if headers:
        request_headers.update(headers)

    last_error = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=35) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Источник данных недоступен: {last_error}")

@st.cache_data(ttl=300)
def fetch_market():
    params = urllib.parse.urlencode({
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": COINS_COUNT,
        "page": 1,
        "sparkline": "true",
        "price_change_percentage": "1h,24h,7d,30d",
    })

    key = st.secrets.get("COINGECKO_API_KEY", "")
    headers = {"x-cg-demo-api-key": key} if key else {}
    url = f"https://api.coingecko.com/api/v3/coins/markets?{params}"

    data = get_json(url, headers=headers)
    if not isinstance(data, list):
        raise RuntimeError("CoinGecko вернул неожиданный ответ")
    return data

@st.cache_data(ttl=1800)
def fetch_fear_greed():
    try:
        item = get_json("https://api.alternative.me/fng/?limit=1")["data"][0]
        return int(item["value"]), item["value_classification"]
    except Exception:
        return 50, "Neutral"

def safe(value, default=0.0):
    return float(value) if value is not None else default

def trend_score(prices):
    if not prices or len(prices) < 24:
        return 0
    series = pd.Series(prices, dtype="float64")
    short = series.tail(24).mean()
    long = series.tail(72).mean() if len(series) >= 72 else series.mean()
    return 1 if short > long * 1.01 else -1 if short < long * 0.99 else 0

def evaluate(coin, fear_value):
    ch1h = safe(coin.get("price_change_percentage_1h_in_currency"))
    ch24 = safe(coin.get("price_change_percentage_24h_in_currency"))
    ch7d = safe(coin.get("price_change_percentage_7d_in_currency"))
    ch30 = safe(coin.get("price_change_percentage_30d_in_currency"))

    market_cap = safe(coin.get("market_cap"))
    volume = safe(coin.get("total_volume"))
    volume_ratio = volume / market_cap if market_cap > 0 else 0

    current = safe(coin.get("current_price"))
    high24 = safe(coin.get("high_24h"))
    low24 = safe(coin.get("low_24h"))
    range24 = (high24 - low24) / current * 100 if current > 0 else 0

    sparkline = (coin.get("sparkline_in_7d") or {}).get("price") or []
    trend7 = trend_score(sparkline)

    score = 50
    reasons = []

    for value, weight, label in [
        (ch1h, 1.0, "1ч"),
        (ch24, 1.5, "24ч"),
        (ch7d, 0.7, "7д"),
        (ch30, 0.25, "30д"),
    ]:
        score += max(-12, min(12, value * weight))
        if abs(value) >= 2:
            reasons.append(f"{label}: {value:+.1f}%")

    if volume_ratio >= 0.15:
        score += 8
        reasons.append("высокая ликвидность")
    elif volume_ratio >= 0.05:
        score += 4
        reasons.append("нормальная ликвидность")
    elif volume_ratio < 0.01:
        score -= 10
        reasons.append("низкая ликвидность")

    score += trend7 * 8
    if trend7 > 0:
        reasons.append("7-дневный тренд вверх")
    elif trend7 < 0:
        reasons.append("7-дневный тренд вниз")

    if range24 > 20:
        score -= 14
        reasons.append("экстремальная волатильность")
    elif range24 > 10:
        score -= 7
        reasons.append("высокая волатильность")

    if fear_value >= 70:
        score += 3 if ch24 > 0 else -3
    elif fear_value <= 30:
        score -= 3 if ch24 < 0 else 2

    nova_score = int(max(0, min(100, round(score))))

    if nova_score >= 75 and ch24 > 0 and trend7 >= 0 and volume_ratio >= 0.01:
        signal = "BUY"
    elif nova_score <= 25 and ch24 < 0 and trend7 <= 0 and volume_ratio >= 0.01:
        signal = "SELL"
    else:
        signal = "WAIT"

    risk = "Высокий" if range24 > 12 or market_cap < 100_000_000 else "Средний" if range24 > 6 else "Низкий"

    return {
        "id": coin.get("id"),
        "Монета": coin.get("name"),
        "Тикер": str(coin.get("symbol", "")).upper(),
        "Место": coin.get("market_cap_rank"),
        "Сигнал": signal,
        "NOVA Score": nova_score,
        "Цена": current,
        "24ч, %": round(ch24, 2),
        "7д, %": round(ch7d, 2),
        "Объём/капитализация": round(volume_ratio, 4),
        "Риск": risk,
        "Причина": "; ".join(reasons[:5]) or "нет сильного преимущества",
    }

def init_state():
    if "balance" not in st.session_state:
        st.session_state.balance = START_BALANCE
    if "positions" not in st.session_state:
        st.session_state.positions = {}
    if "trades" not in st.session_state:
        st.session_state.trades = []
    if "equity_history" not in st.session_state:
        st.session_state.equity_history = []

def close_position(symbol, price, reason):
    position = st.session_state.positions[symbol]
    if position["side"] == "BUY":
        pnl = (price - position["entry"]) * position["qty"]
    else:
        pnl = (position["entry"] - price) * position["qty"]

    st.session_state.balance += position["allocated"] + pnl
    st.session_state.trades.append({
        "Время": datetime.now(timezone.utc).isoformat(),
        "Тикер": symbol,
        "Тип": "CLOSE",
        "Сторона": position["side"],
        "Цена": round(price, 8),
        "Количество": position["qty"],
        "PnL": round(pnl, 4),
        "Причина": reason,
        "Баланс": round(st.session_state.balance, 2),
    })
    del st.session_state.positions[symbol]

def open_position(row):
    if len(st.session_state.positions) >= MAX_POSITIONS:
        return
    symbol = row["Тикер"]
    if symbol in st.session_state.positions:
        return

    allocation = min(
        st.session_state.balance * POSITION_SHARE,
        st.session_state.balance / max(1, MAX_POSITIONS - len(st.session_state.positions))
    )
    if allocation < 10:
        return

    price = float(row["Цена"])
    qty = allocation / price
    side = row["Сигнал"]

    stop = price * (1 - STOP_LOSS_PCT) if side == "BUY" else price * (1 + STOP_LOSS_PCT)
    take = price * (1 + TAKE_PROFIT_PCT) if side == "BUY" else price * (1 - TAKE_PROFIT_PCT)

    st.session_state.balance -= allocation
    st.session_state.positions[symbol] = {
        "side": side,
        "entry": price,
        "qty": qty,
        "allocated": allocation,
        "stop": stop,
        "take": take,
        "score": row["NOVA Score"],
        "name": row["Монета"],
    }
    st.session_state.trades.append({
        "Время": datetime.now(timezone.utc).isoformat(),
        "Тикер": symbol,
        "Тип": "OPEN",
        "Сторона": side,
        "Цена": round(price, 8),
        "Количество": qty,
        "PnL": 0,
        "Причина": f"NOVA Score {row['NOVA Score']}",
        "Баланс": round(st.session_state.balance, 2),
    })

def update_portfolio(scan_df):
    prices = {row["Тикер"]: float(row["Цена"]) for _, row in scan_df.iterrows()}

    for symbol in list(st.session_state.positions.keys()):
        if symbol not in prices:
            continue
        price = prices[symbol]
        p = st.session_state.positions[symbol]

        if p["side"] == "BUY":
            if price <= p["stop"]:
                close_position(symbol, price, "STOP")
            elif price >= p["take"]:
                close_position(symbol, price, "TAKE")
        else:
            if price >= p["stop"]:
                close_position(symbol, price, "STOP")
            elif price <= p["take"]:
                close_position(symbol, price, "TAKE")

    candidates = scan_df[
        scan_df["Сигнал"].isin(["BUY", "SELL"])
        & scan_df["Риск"].isin(["Низкий", "Средний"])
    ].sort_values("NOVA Score", ascending=False)

    for _, row in candidates.iterrows():
        if len(st.session_state.positions) >= MAX_POSITIONS:
            break
        open_position(row)

    equity = st.session_state.balance
    for symbol, p in st.session_state.positions.items():
        price = prices.get(symbol, p["entry"])
        current_value = p["qty"] * price
        if p["side"] == "BUY":
            equity += current_value
        else:
            equity += p["allocated"] + (p["entry"] - price) * p["qty"]

    st.session_state.equity_history.append({
        "Время": datetime.now(timezone.utc),
        "Капитал": round(equity, 2),
    })

def current_equity(scan_df):
    prices = {row["Тикер"]: float(row["Цена"]) for _, row in scan_df.iterrows()}
    equity = st.session_state.balance
    floating = 0.0

    for symbol, p in st.session_state.positions.items():
        price = prices.get(symbol, p["entry"])
        if p["side"] == "BUY":
            pnl = (price - p["entry"]) * p["qty"]
        else:
            pnl = (p["entry"] - price) * p["qty"]
        floating += pnl
        equity += p["allocated"] + pnl

    return equity, floating

init_state()
fear_value, fear_label = fetch_fear_greed()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Fear & Greed", f"{fear_value} — {fear_label}")
c2.metric("Стартовый капитал", f"${START_BALANCE:.2f}")
c3.metric("Макс. позиций", MAX_POSITIONS)
c4.metric("Риск/доходность", f"{STOP_LOSS_PCT*100:.0f}% / {TAKE_PROFIT_PCT*100:.0f}%")

if st.button("Сканировать и обновить демо-сделки", type="primary"):
    try:
        market = fetch_market()
        rows = [evaluate(coin, fear_value) for coin in market]
        st.session_state.scan_df = pd.DataFrame(rows)
        update_portfolio(st.session_state.scan_df)
        st.success("Сканирование и Paper Trading обновлены.")
    except Exception as exc:
        st.error(str(exc))

if "scan_df" in st.session_state:
    scan_df = st.session_state.scan_df

    equity, floating = current_equity(scan_df)
    realized = sum(t["PnL"] for t in st.session_state.trades if t["Тип"] == "CLOSE")

    a, b, c, d = st.columns(4)
    a.metric("Свободный баланс", f"${st.session_state.balance:.2f}")
    b.metric("Капитал", f"${equity:.2f}")
    c.metric("Плавающий PnL", f"${floating:.2f}")
    d.metric("Реализованный PnL", f"${realized:.2f}")

    st.subheader("Открытые виртуальные позиции")
    if st.session_state.positions:
        positions = []
        prices = {row["Тикер"]: float(row["Цена"]) for _, row in scan_df.iterrows()}
        for symbol, p in st.session_state.positions.items():
            current = prices.get(symbol, p["entry"])
            pnl = (current - p["entry"]) * p["qty"] if p["side"] == "BUY" else (p["entry"] - current) * p["qty"]
            positions.append({
                "Монета": p["name"],
                "Тикер": symbol,
                "Сторона": p["side"],
                "Вход": round(p["entry"], 8),
                "Текущая": round(current, 8),
                "Стоп": round(p["stop"], 8),
                "Тейк": round(p["take"], 8),
                "NOVA Score": p["score"],
                "PnL": round(pnl, 4),
            })
        st.dataframe(pd.DataFrame(positions), use_container_width=True, hide_index=True)
    else:
        st.info("Открытых позиций нет.")

    st.subheader("Лучшие сигналы")
    signals = scan_df[scan_df["Сигнал"].isin(["BUY", "SELL"])].sort_values("NOVA Score", ascending=False)
    st.dataframe(signals.head(20), use_container_width=True, hide_index=True)

    st.subheader("История сделок")
    if st.session_state.trades:
        trades_df = pd.DataFrame(st.session_state.trades)
        st.dataframe(trades_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Скачать сделки CSV",
            trades_df.to_csv(index=False).encode("utf-8-sig"),
            "nova_paper_trades.csv",
            "text/csv",
        )

    if st.session_state.equity_history:
        st.subheader("Кривая капитала")
        equity_df = pd.DataFrame(st.session_state.equity_history).set_index("Время")
        st.line_chart(equity_df)

    with st.expander("Полный рейтинг 150 монет"):
        st.dataframe(
            scan_df.sort_values("NOVA Score", ascending=False),
            use_container_width=True,
            hide_index=True,
        )

if st.button("Сбросить демо-портфель"):
    st.session_state.balance = START_BALANCE
    st.session_state.positions = {}
    st.session_state.trades = []
    st.session_state.equity_history = []
    st.success("Демо-портфель сброшен.")

st.warning(
    "Важно: Streamlit Community Cloud не выполняет код 24/7 без внешнего планировщика. "
    "Эта версия обновляет сделки при нажатии кнопки. Следующий этап — подключение постоянного сервера."
)
