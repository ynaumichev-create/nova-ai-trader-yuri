
import json
import time
import urllib.parse
import urllib.request

import pandas as pd
import streamlit as st

COINS_COUNT = 150

st.set_page_config(page_title="NOVA Scanner v0.7", layout="wide")
st.title("NOVA AI Trader v0.7 — Alerts")
st.caption("Сканер 150 монет, избранное и Telegram-уведомления. Сделки не открываются.")

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

def post_form(url, payload):
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8")

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

    if nova_score >= 72 and ch24 > 0 and trend7 >= 0:
        signal = "BUY"
    elif nova_score <= 28 and ch24 < 0 and trend7 <= 0:
        signal = "SELL"
    else:
        signal = "WAIT"

    risk = "Высокий" if range24 > 12 or market_cap < 100_000_000 else "Средний" if range24 > 6 else "Низкий"

    return {
        "Место": coin.get("market_cap_rank"),
        "Монета": coin.get("name"),
        "Тикер": str(coin.get("symbol", "")).upper(),
        "Сигнал": signal,
        "NOVA Score": nova_score,
        "Цена, $": current,
        "1ч, %": round(ch1h, 2),
        "24ч, %": round(ch24, 2),
        "7д, %": round(ch7d, 2),
        "30д, %": round(ch30, 2),
        "Объём/капитализация": round(volume_ratio, 4),
        "Риск": risk,
        "Причина": "; ".join(reasons[:5]) or "нет сильного преимущества",
    }

def send_telegram(rows):
    token = st.secrets.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False, "Telegram не настроен"

    if rows.empty:
        text = "NOVA: сильных сигналов сейчас нет."
    else:
        lines = ["NOVA — лучшие сигналы:"]
        for _, row in rows.head(10).iterrows():
            lines.append(
                f"{row['Тикер']} | {row['Сигнал']} | "
                f"Score {row['NOVA Score']} | 24ч {row['24ч, %']:+.2f}%"
            )
        text = "\n".join(lines)

    post_form(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": text},
    )
    return True, "Отправлено"

fear_value, fear_label = fetch_fear_greed()

c1, c2, c3 = st.columns(3)
c1.metric("Fear & Greed", f"{fear_value} — {fear_label}")
c2.metric("Монет", COINS_COUNT)
c3.metric("Режим", "Бесплатный")

with st.sidebar:
    st.header("Фильтры")
    min_score = st.slider("Минимальный NOVA Score", 0, 100, 68)
    signal_filter = st.multiselect(
        "Сигналы",
        ["BUY", "SELL", "WAIT"],
        default=["BUY", "SELL"],
    )
    max_rank = st.slider("Макс. место по капитализации", 10, COINS_COUNT, 100)
    min_volume_ratio = st.number_input(
        "Мин. объём/капитализация",
        min_value=0.0,
        max_value=1.0,
        value=0.01,
        step=0.01,
    )
    alert_only_low_risk = st.checkbox("Уведомлять только низкий/средний риск", value=True)

if "scan_df" not in st.session_state:
    st.session_state.scan_df = None

if st.button("Сканировать рынок", type="primary"):
    try:
        market = fetch_market()
        progress = st.progress(0)
        rows = []

        for index, coin in enumerate(market, start=1):
            rows.append(evaluate(coin, fear_value))
            progress.progress(index / len(market))

        st.session_state.scan_df = pd.DataFrame(rows)
    except Exception as exc:
        st.error(str(exc))

df = st.session_state.scan_df

if df is not None:
    filtered = df[
        (df["NOVA Score"] >= min_score)
        & (df["Сигнал"].isin(signal_filter))
        & (df["Место"] <= max_rank)
        & (df["Объём/капитализация"] >= min_volume_ratio)
    ].copy()

    if alert_only_low_risk:
        filtered = filtered[filtered["Риск"].isin(["Низкий", "Средний"])]

    filtered = filtered.sort_values(["NOVA Score", "24ч, %"], ascending=[False, False])

    st.subheader("Лучшие возможности")
    if filtered.empty:
        st.info("По текущим фильтрам сильных сигналов нет.")
    else:
        st.dataframe(filtered, use_container_width=True, hide_index=True)

    a, b, c, d = st.columns(4)
    a.metric("BUY", int((df["Сигнал"] == "BUY").sum()))
    b.metric("SELL", int((df["Сигнал"] == "SELL").sum()))
    c.metric("WAIT", int((df["Сигнал"] == "WAIT").sum()))
    d.metric("Под фильтром", len(filtered))

    if st.button("Отправить лучшие сигналы в Telegram"):
        try:
            ok, message = send_telegram(filtered)
            st.success(message) if ok else st.warning(message)
        except Exception as exc:
            st.error(f"Telegram: {exc}")

    st.download_button(
        "Скачать рейтинг CSV",
        df.sort_values("NOVA Score", ascending=False).to_csv(index=False).encode("utf-8-sig"),
        "nova_150_alerts.csv",
        "text/csv",
    )

    st.subheader("Полный рейтинг")
    st.dataframe(
        df.sort_values("NOVA Score", ascending=False),
        use_container_width=True,
        hide_index=True,
    )

with st.expander("Статус Telegram"):
    st.write({
        "TELEGRAM_BOT_TOKEN": bool(st.secrets.get("TELEGRAM_BOT_TOKEN", "")),
        "TELEGRAM_CHAT_ID": bool(st.secrets.get("TELEGRAM_CHAT_ID", "")),
    })

st.warning("Сигналы исследовательские. Реальные ордера не отправляются.")
