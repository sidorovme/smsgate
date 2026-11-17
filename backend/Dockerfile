FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    SMSGATE_CONFIG=/data/gateways.yaml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# gateways.yaml и sms.db лежат в /data (volume)
VOLUME /data
CMD ["python", "main.py"]
