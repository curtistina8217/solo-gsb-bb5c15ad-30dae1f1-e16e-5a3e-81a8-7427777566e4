# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖,利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷业务代码
COPY app ./app

EXPOSE 8000

# 容器启动即提供 API,0.0.0.0 保证容器外可访问
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
