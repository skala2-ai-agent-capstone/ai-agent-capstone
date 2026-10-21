# business-mcp·rag-mcp·db-init(적재기)·verify가 같은 이미지를 쓰고 docker-compose의 command로 구분한다.
# 3.13은 쓰지 않는다: chromadb-client 0.6.3이 numpy<2를 요구하는데 numpy 1.26.x는 3.13 휠이 없어 소스 빌드가 실패한다
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    ANONYMIZED_TELEMETRY=False

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY business_mcp ./business_mcp
COPY rag_mcp ./rag_mcp
COPY db ./db
COPY tests ./tests
COPY data ./data

RUN useradd --system --uid 10001 app
USER app

EXPOSE 8001 8002
CMD ["python", "-m", "business_mcp.server"]
