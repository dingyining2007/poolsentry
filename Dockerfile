FROM python:3.11-slim
WORKDIR /app
COPY . /app
ENV PYTHONUNBUFFERED=1
# 零第三方依赖：只用 stdlib + 环境里已有的 RPC 访问
CMD ["python3", "mcp_server.py"]
