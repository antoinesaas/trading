FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY dashboard ./dashboard
COPY pine ./pine
COPY data ./data
RUN useradd --create-home bot && mkdir -p logs reports && chown -R bot:bot /app
USER bot

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", 8000)}/health', timeout=4)"
CMD ["python", "-m", "app", "serve", "--host", "0.0.0.0"]
