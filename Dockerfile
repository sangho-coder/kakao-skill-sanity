FROM python:3.10-slim-bullseye
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PORT=8080
CMD ["sh","-c","exec python -m http.server $PORT"]
