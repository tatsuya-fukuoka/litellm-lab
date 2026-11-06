FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
EXPOSE 9000
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port 9000 --workers ${MOCK_WORKERS:-1} --log-level warning"]
