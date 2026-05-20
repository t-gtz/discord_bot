FROM python:3.11-alpine

RUN apk add --no-cache ffmpeg gcc musl-dev libffi-dev libsodium-dev openssl-dev opus opus-dev

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]