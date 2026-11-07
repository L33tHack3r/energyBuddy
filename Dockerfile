FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN groupadd --system coach \
    && useradd --system --gid coach --home-dir /app coach \
    && mkdir -p /state \
    && chown coach:coach /state

ENV DATABASE_PATH=/state/energy_coach.sqlite3 \
    DATA_DIR=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
USER coach
CMD ["python", "backend/server.py"]
