FROM python:3.12-slim-bookworm

LABEL maintainer="CLRobotics Security Systems"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencias de sistema (psycopg2, reportlab)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential libpq-dev wget \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

COPY requirements/common.txt /app/requirements/common.txt
RUN pip install --timeout=1000 -r /app/requirements/common.txt

COPY . /app/

COPY docker/web/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
