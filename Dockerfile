# szlib-mcp —— 深圳图书馆数字资源检索 MCP
# 需要真实浏览器（Firefox），因此镜像包含 Playwright 及其系统依赖。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    SZLIB_PROFILE_DIR=/data/profile \
    SZLIB_VERIFY_PORT=8765

# Playwright 的 driver 是 node 二进制，依赖 libatomic1；
# slim 镜像里没有，缺了会在 `playwright install` 阶段 exit 127。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl ca-certificates libatomic1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --upgrade pip \
 && pip install . \
 && pip install "playwright==1.65.2" \
 && python -m playwright install --with-deps firefox \
 && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /data/profile

EXPOSE 8765
CMD ["python", "-m", "szlib_mcp.server"]
