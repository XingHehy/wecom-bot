FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 先安装依赖（更利于 Docker layer 缓存）
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r /app/requirements.txt && \
    python -m playwright install --with-deps chromium --only-shell

# 拷贝源码
COPY . /app

# 确保运行时目录存在
RUN mkdir -p /app/temp_media /app/logs

# 默认端口（建议与 config.yaml 的 deployment.port 保持一致）
EXPOSE 4455

# 运行入口：所有运行时代码在 app 包内
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4455"]

