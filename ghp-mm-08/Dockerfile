ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:latest
FROM ${BUILD_FROM}

# Устанавливаем Python и нужные библиотеки
RUN apk add --no-cache python3 py3-pip py3-virtualenv

# Создаём рабочую директорию и копируем файлы
WORKDIR /usr/src/app
COPY . .

# Создаём виртуальное окружение и устанавливаем зависимости
RUN python3 -m venv /usr/src/app/venv \
    && . /usr/src/app/venv/bin/activate \
    && pip install --no-cache-dir -r requirements.txt \
    && deactivate

# Разрешаем выполнение скрипта запуска
RUN chmod +x /usr/src/app/run.sh

# Указываем команду запуска
CMD [ "/usr/src/app/run.sh" ]
