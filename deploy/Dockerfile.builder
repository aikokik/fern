# Image for the builder (Claude Code CLI + bridge) and the shipper (git + GitHub). Same image, different commands
# and different secrets per container (see compose.yaml).
FROM python:3.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git rsync curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /bin/uv
RUN useradd --create-home --uid 10001 builder \
    && mkdir -p /workspace /repos /state/home /state/claude \
    && chown -R builder /workspace /repos /state
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
USER builder
# Claude Code CLI via the native installer (into ~/.local/bin). Auto-update is off: the root fs is read-only.
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH=/home/builder/.local/bin:/app/.venv/bin:$PATH \
    HOME=/state/home \
    CLAUDE_CONFIG_DIR=/state/claude \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    UV_CACHE_DIR=/workspace/.uv-cache \
    PYTHONDONTWRITEBYTECODE=1
