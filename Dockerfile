FROM python:3.11-slim

# 不寫 .pyc、即時輸出 log
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先裝相依，善用 Docker 快取
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼
COPY pionexbot/ ./pionexbot/
COPY main.py ./

# 持久化交易記錄與日誌
VOLUME ["/app/data", "/app/logs"]

# 預設啟動策略機器人；可在 docker run / compose 覆寫成 run-webhook 等
ENTRYPOINT ["python", "main.py"]
CMD ["run-strategy"]
