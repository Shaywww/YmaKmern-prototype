FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY packages/ ./packages/
COPY tests/ ./tests/

RUN pip install --no-cache-dir . astrbot

EXPOSE 8000 6189

ENV PYTHONUNBUFFERED=1
ENV DUDUDA_ENV=production

CMD ["python", "-m", "uvicorn", "dududa.control_plane.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--factory"]
