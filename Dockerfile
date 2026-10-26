FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    DP_DATA_DIR=/app/data

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

VOLUME ["/app/data"]
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
