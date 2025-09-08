# diag-triage service image.
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY triage ./triage
RUN pip install --no-cache-dir .

EXPOSE 8080
# Default: the ingestion/triage API. Override CMD for the MCP server.
CMD ["diag-triage", "serve"]
