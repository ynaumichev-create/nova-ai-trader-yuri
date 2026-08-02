
import json
import urllib.request
from datetime import datetime, timezone

import streamlit as st

st.set_page_config(page_title="NOVA AI Trader — AI Brain", layout="wide")
st.title("NOVA AI Trader — AI Brain")
st.caption("Совет из нескольких моделей. Пока только анализ, без реальных сделок.")

DEFAULT_MARKET = {
    "symbol": "BTC-USD",
    "price": 0,
    "trend_score": 0,
    "momentum_score": 0,
    "volume_score": 0,
    "risk_score": 0,
    "notes": ""
}

def post_json(url, headers, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))

def safe_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end+1]
    return json.loads(text)

SYSTEM = """Ты независимый риск-аналитик. Не обещай прибыль.
Верни только JSON:
{"decision":"BUY|SELL|WAIT","confidence":0-100,"risk":0-100,"reason":"кратко"}"""

def prompt_for(market):
    return f"""Оцени торговую идею:
{json.dumps(market, ensure_ascii=False)}
Учитывай, что это демо-анализ. При недостатке данных выбирай WAIT."""

def call_openai(market):
    key = st.secrets.get("OPENAI_API_KEY", "")
    model = st.secrets.get("OPENAI_MODEL", "gpt-5")
    if not key:
        return None
    data = post_json(
        "https://api.openai.com/v1/responses",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {"model": model, "instructions": SYSTEM, "input": prompt_for(market)}
    )
    text = data.get("output_text")
    if not text:
        parts = []
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    parts.append(content.get("text", ""))
        text = "\n".join(parts)
    return safe_json(text)

def call_claude(market):
    key = st.secrets.get("ANTHROPIC_API_KEY", "")
    model = st.secrets.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    if not key:
        return None
    data = post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        {
            "model": model,
            "max_tokens": 400,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": prompt_for(market)}],
        },
    )
    text = "".join(x.get("text", "") for x in data.get("content", []) if x.get("type") == "text")
    return safe_json(text)

def call_gemini(market):
    key = st.secrets.get("GEMINI_API_KEY", "")
    model = st.secrets.get("GEMINI_MODEL", "gemini-2.5-flash")
    if not key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    data = post_json(
        url,
        {"Content-Type": "application/json"},
        {
            "system_instruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"parts": [{"text": prompt_for(market)}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
    )
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return safe_json(text)

def call_xai(market):
    key = st.secrets.get("XAI_API_KEY", "")
    model = st.secrets.get("XAI_MODEL", "grok-4")
    if not key:
        return None
    data = post_json(
        "https://api.x.ai/v1/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt_for(market)},
            ],
            "temperature": 0.1,
        },
    )
    return safe_json(data["choices"][0]["message"]["content"])

def fallback_agent(name, market):
    score = (
        market["trend_score"]
        + market["momentum_score"]
        + market["volume_score"]
        + market["risk_score"]
    )
    decision = "BUY" if score >= 4 else "SELL" if score <= -4 else "WAIT"
    return {
        "decision": decision,
        "confidence": min(80, 50 + abs(score) * 6),
        "risk": max(20, min(90, 50 - market["risk_score"] * 10)),
        "reason": f"{name}: резервная оценка по локальным баллам"
    }

def arbitrate(results):
    votes = {"BUY": 0, "SELL": 0, "WAIT": 0}
    weighted = {"BUY": 0.0, "SELL": 0.0, "WAIT": 0.0}
    for r in results:
        decision = r["decision"]
        votes[decision] += 1
        weighted[decision] += r["confidence"] * (1 - r["risk"] / 100)

    winner = max(weighted, key=weighted.get)
    avg_risk = sum(r["risk"] for r in results) / len(results)
    if votes[winner] < 2 or avg_risk >= 70:
        winner = "WAIT"

    confidence = round(sum(r["confidence"] for r in results if r["decision"] == winner) / max(votes[winner], 1))
    return winner, confidence, round(avg_risk)

symbol = st.text_input("Инструмент", "BTC-USD")
price = st.number_input("Цена", min_value=0.0, value=0.0)
c1, c2, c3, c4 = st.columns(4)
trend = c1.slider("Trend", -2, 2, 0)
momentum = c2.slider("Momentum", -2, 2, 0)
volume = c3.slider("Volume", -2, 2, 0)
risk = c4.slider("Risk", -2, 1, 0)
notes = st.text_area("Дополнительные данные", "")

market = {
    "symbol": symbol,
    "price": price,
    "trend_score": trend,
    "momentum_score": momentum,
    "volume_score": volume,
    "risk_score": risk,
    "notes": notes,
    "timestamp": datetime.now(timezone.utc).isoformat(),
}

if st.button("Запустить совет ИИ", type="primary"):
    agents = [
        ("ChatGPT", call_openai),
        ("Claude", call_claude),
        ("Gemini", call_gemini),
        ("Grok", call_xai),
    ]

    results = []
    for name, fn in agents:
        try:
            answer = fn(market)
            if answer is None:
                answer = fallback_agent(name, market)
                answer["source"] = "fallback"
            else:
                answer["source"] = "API"
            answer["agent"] = name
            results.append(answer)
        except Exception as e:
            answer = fallback_agent(name, market)
            answer["source"] = f"fallback: {e}"
            answer["agent"] = name
            results.append(answer)

    decision, confidence, avg_risk = arbitrate(results)

    a, b, c = st.columns(3)
    a.metric("Решение NOVA", decision)
    b.metric("Уверенность", f"{confidence}%")
    c.metric("Средний риск", f"{avg_risk}%")

    st.dataframe(results, use_container_width=True)
    st.info("Сделка не исполняется автоматически.")

with st.expander("Какие ключи подключены"):
    st.write({
        "OpenAI": bool(st.secrets.get("OPENAI_API_KEY", "")),
        "Anthropic": bool(st.secrets.get("ANTHROPIC_API_KEY", "")),
        "Gemini": bool(st.secrets.get("GEMINI_API_KEY", "")),
        "xAI": bool(st.secrets.get("XAI_API_KEY", "")),
    })
