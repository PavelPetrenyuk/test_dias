FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir . \
    && addgroup --system app \
    && adduser --system --ingroup app --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /data

USER app

CMD ["uvicorn", "article_masks.api:app", "--host", "0.0.0.0", "--port", "8000"]
