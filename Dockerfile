FROM python:3.10-slim

# Устанавливаем и FFmpeg, и Aria2
RUN apt-get update && \
    apt-get install -y ffmpeg aria2 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
