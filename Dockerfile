FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tashkent \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
# Дефолтные база знаний и стиль: при первом запуске копируются в DATA_DIR/kb, дальше их меняет владелец.
COPY kb ./kb

# Непривилегированный пользователь; data/ монтируется томом и должен быть ему доступен.
RUN useradd --create-home --uid 10001 kotib \
    && mkdir -p /app/data \
    && chown -R kotib:kotib /app
USER kotib

VOLUME ["/app/data"]

CMD ["python", "-m", "bot.main"]
