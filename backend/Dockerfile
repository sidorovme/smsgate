FROM python:3.12-slim

# Конфиг: либо /data/gateways.yaml, либо переменные окружения (см. config.py)
ENV PYTHONUNBUFFERED=1 \
    SMSGATE_CONFIG=/data/gateways.yaml \
    DB_PATH=/data/sms.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# sms.db лежит в /data (volume)
VOLUME /data
CMD ["python", "main.py"]
