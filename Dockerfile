FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN adduser --disabled-password --gecos "" appuser \
    && mkdir -p /config /downloads \
    && chown -R appuser:appuser /app /config /downloads

USER appuser

EXPOSE 8080

CMD ["python", "-m", "app.main"]
