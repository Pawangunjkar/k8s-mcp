FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    MCP_TRANSPORT=streamable-http \
    HOME=/tmp \
    K8S_NAMESPACE=default \
    K8S_SESSION=default

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE* ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && groupadd -r appuser \
    && useradd -r -g appuser -d /tmp -s /sbin/nologin appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health')" || exit 1

CMD ["python", "-m", "k8s_cloud_deploy_mcp.server"]
