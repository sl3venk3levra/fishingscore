# Fischis – alles drin, was Un-raid lesen kann
FROM python:3.11-slim

ARG APP_VERSION="0.1.0"
ARG ICON_URL="http://admin-gs.de/mtSg4fxwMw8xg3oJx54UjtvzQq8U0S/docker_label.png"

# --- Pakete
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential libssl-dev libffi-dev curl && \
    rm -rf /var/lib/apt/lists/*

# --- Code
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir \
        paho-mqtt pyyaml flask requests ephem \
        python-dotenv beautifulsoup4 lxml tzdata 

# --- Labels
LABEL net.unraid.docker.managed="dockerman" \
      net.unraid.docker.icon="${ICON_URL}" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.label-schema.version="${APP_VERSION}"

EXPOSE 5000
CMD ["python", "mqtt.py"]
