# Optional persistent per-agent image with both Python and Node.js.
# Build it, then set AVRONE_DOCKER_IMAGE=avrone-agent-node:22.
FROM node:22-bookworm-slim AS node_runtime
FROM python:3.12-slim-bookworm

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/bin/npm /usr/local/bin/npm
COPY --from=node_runtime /usr/local/bin/npx /usr/local/bin/npx
COPY --from=node_runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && python --version \
    && node --version \
    && npm --version

WORKDIR /workspace
CMD ["sleep", "infinity"]
