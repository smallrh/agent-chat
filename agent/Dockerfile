# 多阶段构建：依赖装到 builder 阶段的 --user 目录，再拷进运行镜像，
# 运行镜像里没有 pip / 编译工具链，体积和攻击面都更小。

FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .

# --user 装到 /root/.local，方便整体拷走
RUN pip install --user -r requirements.txt


FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/root/.local/bin:${PATH}"

WORKDIR /app

# 只拷依赖，不拷 pip 缓存与构建中间产物
COPY --from=builder /root/.local /root/.local
COPY app/ ./app/
COPY migrations/ ./migrations/

# 非 root 运行（镜像里默认没有这个用户，先建一个）
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# healthcheck 用标准库探测 /healthz，避免为了 curl 装额外包。
# 注意：这里探的是 liveness（进程活着即可），故意不探依赖 ——
# 依赖抖动不应该触发容器重启。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)" || exit 1

# 单进程；多副本用 --workers 或直接起多个容器（注意 cancel token 与
# task_event_bus 都是进程内的，多副本前先看 ADR-008 / ADR-009 的限制）
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
