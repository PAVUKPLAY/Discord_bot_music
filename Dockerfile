FROM python:3.11-slim

# УСТАНОВКА FFMPEG (самое важное!)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Очищаем BOM и невидимые символы из requirements.txt (оставляем как у вас)
RUN sed -i 's/^\xEF\xBB\xBF//' requirements.txt 2>/dev/null || true && \
    sed -i 's/^\xFF\xFE//' requirements.txt 2>/dev/null || true && \
    sed -i 's/^\xFE\xFF//' requirements.txt 2>/dev/null || true && \
    tr -d '\r\0' < requirements.txt > requirements.txt.tmp && mv requirements.txt.tmp requirements.txt 2>/dev/null || true
# Проверяем содержимое
RUN cat requirements.txt && echo '--- Requirements.txt содержимое выше ---'
# Устанавливаем зависимости из requirements.txt
RUN set -e && \
    echo 'Начинаем установку зависимостей из requirements.txt...' && \
    if [ ! -f requirements.txt ] || [ ! -s requirements.txt ]; then \
        echo 'WARNING: requirements.txt пуст или не существует'; \
    else \
        while IFS= read -r line || [ -n "$line" ]; do \
            [ -z "$line" ] && continue; \
            line=$(echo "$line" | sed 's/^\xEF\xBB\xBF//' | sed 's/^\xFF\xFE//' | sed 's/^\xFE\xFF//' | tr -d '\r\0' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'); \
            [ -z "$line" ] && continue; \
            case "$line" in \
              \#*) continue ;; \
            esac; \
            echo "=== Устанавливаем: $line ===" && \
            if echo "$line" | grep -qiE '^(sqlite3|json|os|sys|time|datetime|re|random|math|logging|asyncio|collections|itertools|functools|operator|pathlib|urllib|http|socket|ssl|hashlib|base64|uuid|threading|multiprocessing|queue|concurrent|subprocess|shutil|tempfile|pickle|copy|weakref|gc|ctypes|struct|array|binascii|codecs|encodings|locale|gettext|argparse|configparser|csv|io|textwrap|string|unicodedata|readline|rlcompleter)$'; then \
                echo "ℹ️  Пропускаем встроенный модуль Python: $line (не требует установки)"; \
                continue; \
            fi && \
            if ! pip install --no-cache-dir "$line"; then \
                echo "ERROR: Не удалось установить $line" && exit 1; \
            else \
                echo "✅ Успешно установлен: $line"; \
            fi; \
        done < requirements.txt; \
    fi && \
    echo 'Установка завершена' && \
    pip list

# Очищаем pip кеш
RUN pip cache purge || true

# Копируем код приложения
COPY . .

# Директория для постоянных данных
ENV DATA_DIR=/app/data
RUN mkdir -p /app/data && chmod 777 /app/data

# Запуск бота
CMD ["python", "main.py"]