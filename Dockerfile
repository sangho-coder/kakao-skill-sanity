FROM python:3.10-slim-bullseye
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
# $PORT 확장 보장: sh -c 필수
CMD ["sh","-c","exec gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 30 --graceful-timeout 10 --keep-alive 65 --log-level info --error-logfile - --access-logfile -"]
