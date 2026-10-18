# ===== MusicArchiver (Python 后端 + 洛雪音源沙箱) =====
# 洛雪音源脚本(JS)通过 node 子进程 + vm 沙箱运行; 仅需 node 二进制(apt 安装, 无 npm)
FROM python:3.14-slim-bookworm
WORKDIR /app

# slim-bookworm 自带 python + pip, 只需装 nodejs; Debian Bookworm 自带 node 18 LTS
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# pip 国内镜像(默认阿里云, 构建失败可 --build-arg PIP_INDEX_URL=... 覆盖)
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_INDEX_URL=$PIP_INDEX_URL PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt ./
# 三级源回退: 阿里云 -> 清华 -> 官方 PyPI, 任一成功即继续; 装完自检可导入
RUN pip install --no-cache-dir --timeout 60 --retries 3 -r requirements.txt \
  || pip install --no-cache-dir --timeout 60 --retries 3 -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt \
  || pip install --no-cache-dir --timeout 60 --retries 3 -i https://pypi.org/simple/ -r requirements.txt \
  && python -c "import fastapi, uvicorn; print('deps ok')"

COPY app/ ./app/
COPY bridge.mjs ./
COPY crypto-js.js ./
COPY web/static/ ./web/static/

ENV PORT=5245 DATA_DIR=/data CONCURRENCY=3
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 5245
CMD ["python", "-m", "app.main"]
