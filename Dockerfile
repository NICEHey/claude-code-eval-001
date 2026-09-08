# 水文监测服务：仅依赖 Python 3.11 标准库，无外部服务/数据库依赖。
FROM python:3.11-slim

WORKDIR /app

# 业务代码与启动脚本（测试也一并打入，便于容器内自检）
COPY app/ ./app/
COPY tests/ ./tests/
COPY fixtures/ ./fixtures/
COPY run.sh ./run.sh
RUN chmod +x ./run.sh && mkdir -p /app/data

ENV HOST=0.0.0.0 \
    PORT=8000 \
    DB_PATH=/app/data/hydro.db

EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

CMD ["./run.sh"]
