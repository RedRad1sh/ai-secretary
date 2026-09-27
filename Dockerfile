FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg — конвертация голосовых сообщений для ASR (на всякий случай)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot/ ./bot/
COPY scripts/ ./scripts/

RUN useradd -m botuser && mkdir -p /app/data && chown -R botuser /app
USER botuser

CMD ["python", "-m", "bot.main"]
