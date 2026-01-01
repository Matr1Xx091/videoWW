# Используем легкий Python
FROM python:3.10-slim

# Обновляем систему и устанавливаем FFmpeg (самое важное!)
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Создаем папку для бота
WORKDIR /app

# Копируем список библиотек и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY . .

# Запускаем
CMD ["python", "main.py"]
