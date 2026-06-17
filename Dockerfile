FROM mcr.microsoft.com/playwright/python:v1.53.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/backend
ENV APP_HOST=0.0.0.0
ENV PORT=8000

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY . /app

EXPOSE 8000

CMD ["python", "backend/server.py"]
