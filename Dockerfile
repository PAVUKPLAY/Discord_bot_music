# Используем официальный образ Python
FROM python:3.11-slim

# Отключаем буферизацию вывода логов
ENV PYTHONUNBUFFERED=1

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Обновляем список пакетов и устанавливаем FFmpeg, необходимый для работы с аудио.
# Команды объединены для уменьшения итогового размера образа.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Копируем файл с зависимостями Python и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем ВСЕ файлы вашего бота из текущей папки в рабочую папку контейнера
COPY . .

# Команда для запуска бота при старте контейнера
CMD ["python", "main.py"]
