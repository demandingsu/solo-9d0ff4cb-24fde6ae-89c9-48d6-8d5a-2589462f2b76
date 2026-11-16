# -*- coding: utf-8 -*-
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/water-quality

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite database and the published export tree live in the mounted volume
ENV WQ_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000

# single worker on purpose: revision allocation and the atomic publish step
# rely on the process-wide lock in app/service.py
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
