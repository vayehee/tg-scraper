# syntax=docker/dockerfile:1.6
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

# Install system deps for lxml, cchardet speedups if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libxml2-dev \
    libxslt1.1 libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy app
WORKDIR /app
COPY main.py /app/main.py

# Install Python deps
RUN pip install --upgrade pip \
 && pip install \
      fastapi==0.115.5 \
      uvicorn==0.32.1 \
      httpx==0.27.2 \
      beautifulsoup4==4.12.3 \
      lxml==5.3.0 \
      tenacity==9.0.0 \
      pydantic==2.9.2

EXPOSE 8080

# Cloud Run expects the server to bind $PORT
CMD ["sh","-c","uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
