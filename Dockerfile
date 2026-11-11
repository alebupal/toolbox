FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_ROOT=/host \
    CONFIG_DIR=/config \
    MOVER_DATA_DIR=/config/cli/mover \
    SUBEFTP_DATA_DIR=/config/cli/ftp \
    PORT=8090

RUN apt-get update && apt-get install -y --no-install-recommends \
    rsync \
    curl \
    iproute2 \
    gawk \
    util-linux \
    findutils \
    diffutils \
    ncurses-bin \
    ca-certificates \
    tzdata \
    gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
COPY scripts/ ./scripts/
COPY README.md ./README.md
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /app/scripts/*.sh \
    && ln -sf /app/scripts/toolbox.sh /usr/local/bin/toolbox \
    && ln -sf /app/scripts/mover.sh /usr/local/bin/mover \
    && ln -sf /app/scripts/subeftp.sh /usr/local/bin/subeftp

VOLUME ["/config", "/host"]
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
