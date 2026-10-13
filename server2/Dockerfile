FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8001 \
    HOST=0.0.0.0 \
    MODEL_NAME=Qwen3.5-4B \
    MODEL_PATH=/models/Qwen3.5-4B \
    INTERNAL_API_TOKEN=

RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app

WORKDIR /srv/server2

COPY requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY README.md ./README.md

RUN mkdir -p /models /var/data && chown -R app:app /srv/server2 /models /var/data

USER app
EXPOSE 8001

CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8001}"]
