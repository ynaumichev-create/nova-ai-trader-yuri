
# NOVA AI Trader Cloud

## Как загрузить на GitHub

1. Создайте новый репозиторий: `nova-ai-trader`
2. Нажмите **Add file → Upload files**
3. Загрузите:
   - `app.py`
   - `requirements.txt`
4. Нажмите **Commit changes**

## Как запустить бесплатно

1. Откройте Streamlit Community Cloud
2. Войдите через GitHub
3. Нажмите **Create app**
4. Выберите репозиторий `nova-ai-trader`
5. Main file path: `app.py`
6. Нажмите **Deploy**

## Telegram позже

В настройках приложения → Secrets добавить:

```toml
TELEGRAM_BOT_TOKEN="..."
TELEGRAM_CHAT_ID="..."
```

Пока можно запускать без Telegram.
