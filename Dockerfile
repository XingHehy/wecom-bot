FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 先安装依赖（更利于 Docker layer 缓存）
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r /app/requirements.txt

# 拷贝源码
COPY . /app

# 确保运行时目录存在
RUN mkdir -p /app/temp_media /app/logs

# 默认端口（建议与 config.yaml 的 deployment.port 保持一致）
EXPOSE 4455

# 运行入口：main.py 内部会启动 uvicorn，并在启动时校验 Redis
CMD ["python", "main.py"]

