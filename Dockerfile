# Use slim Python base
FROM python:3.12-slim

# Safer, quieter Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Workdir
WORKDIR /app

# Install deps
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# App code
COPY main.py .
COPY scrpae.py .
COPY gpt.py .
COPY gtranslate.py .
# COPY strings.py .

# Cloud Run port
ENV PORT=8080
EXPOSE 8080

# Entrypoint
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8080"]
