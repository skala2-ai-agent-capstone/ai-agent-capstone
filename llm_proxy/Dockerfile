FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy ./proxy

RUN useradd --system --uid 10001 app
USER app

EXPOSE 4000
CMD ["uvicorn", "proxy.app:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "4000"]
