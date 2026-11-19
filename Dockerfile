# szlib-mcp —— 深圳图书馆数字资源检索 MCP
# 需要真实浏览器（Firefox），因此镜像包含 Playwright 及其系统依赖。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SZLIB_PROFILE_DIR=/data/profile \
    SZLIB_VERIFY_PORT=8765

# Playwright 官方推荐方式安装系统依赖
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --upgrade pip \
 && pip install . \
 && pip install "playwright>=1.45" \
 && python -m playwright install --with-deps firefox \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /data/profile

EXPOSE 8765
CMD ["python", "-m", "szlib_mcp.server"]
