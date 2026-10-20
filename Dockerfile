# Используем официальный Python-образ 
FROM python:3.12.3-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем requirements и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код
COPY . .

# Корень Минцифры нужен для TLS к platform-api2.max.ru. Остальные корни не заменяются.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && cp certs/russian_trusted_root_ca.pem /usr/local/share/ca-certificates/russian_trusted_root_ca.crt \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Команда для запуска бота
CMD ["python", "main.py"]
