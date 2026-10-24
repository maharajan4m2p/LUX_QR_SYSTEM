FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HOST=0.0.0.0
ENV PORT=10000

CMD ["gunicorn", "--bind", "0.0.0.0:10000", "--workers", "1", "--access-logfile", "-", "app:app"]
