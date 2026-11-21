FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/crow-os
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x docker-entrypoint.sh

ENV CROW_HOST=0.0.0.0 \
    CROW_PORT=8765 \
    CROW_OPEN_BROWSER=0 \
    CROW_REPO=/repo \
    OLLAMA_HOST=http://host.docker.internal:11434

EXPOSE 8765
ENTRYPOINT ["./docker-entrypoint.sh"]
