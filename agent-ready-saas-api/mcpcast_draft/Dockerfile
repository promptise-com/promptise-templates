FROM python:3.12-slim-bookworm

WORKDIR /app
COPY pyproject.toml README.md ./
COPY taskboard_mcp ./taskboard_mcp
RUN pip install --no-cache-dir . \
    && useradd --system --create-home --shell /usr/sbin/nologin app
USER app

# The upstream base URL is compiled into taskboard_mcp/config.py; MCPCAST_BASE_URL at run
# time points the image at another environment. Credentials come from the
# environment at run time too (see .env.example) — never bake them into the image.
EXPOSE 8080

# Auth mode 'api-key': MCP clients authenticate with MCPCAST_CLIENT_KEYS;
# the upstream credentials live in MCPCAST_UPSTREAM_TOKENS.
CMD ["taskboard-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8080"]
