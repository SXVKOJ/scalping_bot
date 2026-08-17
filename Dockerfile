FROM python:3.11-bookworm

RUN apt-get update && \
    apt-get install -y curl gnupg build-essential locales && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    locale-gen ru_RU.UTF-8 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

ENV LANG=ru_RU.UTF-8
WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY mexc-sdk-1.0.0 /app/mexc-sdk-1.0.0
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir ./mexc-sdk-1.0.0

COPY . /app/

EXPOSE 8000
