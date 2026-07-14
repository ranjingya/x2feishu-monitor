FROM python:3.13-slim-bookworm

RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ uv

ENV TZ=Asia/Shanghai

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

RUN mkdir -p /data && chown -R nobody:nogroup /data
USER nobody

ENTRYPOINT ["/app/.venv/bin/x2feishu-monitor"]
