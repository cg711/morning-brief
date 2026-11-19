# The server-side writer: headless Claude Code plus the polling loop. Build from the repo root:
#   docker build -f writer_service/Dockerfile .
# Verify the pin exists first: npm view @anthropic-ai/claude-code@<version> version
FROM node:22-slim

ARG CLAUDE_CODE_VERSION=2.1.259

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && npm cache clean --force

ENV DISABLE_AUTOUPDATER=1 \
    HOME=/home/writer \
    PYTHONPATH=/opt/writer \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY writer_service /opt/writer/writer_service
COPY scripts/render_worker_prompt.py /opt/writer/scripts/render_worker_prompt.py
COPY worker/daily-brief-task.template.md worker/deep-dive-task.template.md /opt/writer/worker/

# The `node` user is uid 1000. The home directory is a named volume at runtime (the rest of the image is read-only);
# a new volume inherits this directory's ownership.
RUN mkdir -p /home/writer && chown 1000:1000 /home/writer
USER node
WORKDIR /opt/writer
ENTRYPOINT ["/bin/sh", "/opt/writer/writer_service/entrypoint.sh"]
