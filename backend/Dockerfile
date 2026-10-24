FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY backend/pyproject.toml /app/pyproject.toml
COPY backend/app/ /app/app/
COPY backend/alembic.ini /app/alembic.ini
COPY backend/alembic/ /app/alembic/
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home stova
USER stova
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3)" || exit 1
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
