FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends docker.io git curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY avrone ./avrone
RUN pip install --no-cache-dir .
ENV AVRONE_HOST=0.0.0.0 AVRONE_PORT=8710 AVRONE_WORKSPACE=/srv/workspace AVRONE_DATA_DIR=/srv/data
EXPOSE 8710
CMD ["avrone"]
