FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8002 \
    HOST=0.0.0.0 \
    STORAGE_DATA_DIR=/data

RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app

WORKDIR /srv/server3

COPY requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY README.md ./README.md

RUN mkdir -p /data && chown -R app:app /srv/server3 /data

USER app
EXPOSE 8002

CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8002}"]
