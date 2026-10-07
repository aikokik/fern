FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY skills ./skills
ENV PATH=/app/.venv/bin:$PATH FERN_DATA=/data FERN_SKILLS=/app/skills PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8765
CMD ["fern", "serve", "--host", "0.0.0.0"]
