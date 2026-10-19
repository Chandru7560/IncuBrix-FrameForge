FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

RUN useradd --create-home --uid 10001 scenepilot \
    && mkdir -p /app/data/inputs /app/data/outputs /app/data/render-cache \
    && chown -R scenepilot:scenepilot /app
USER scenepilot

EXPOSE 8000
CMD ["uvicorn", "scenepilot.main:app", "--host", "0.0.0.0", "--port", "8000"]
