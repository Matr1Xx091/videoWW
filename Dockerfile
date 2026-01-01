FROM python:3.10-slim

# Устанавливаем FFmpeg, Aria2 И NODEJS (важно для YouTube!)
RUN apt-get update && \
    apt-get install -y ffmpeg aria2 curl && \
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
