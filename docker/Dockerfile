# Suanpan 网关 Docker 镜像（issue #22）——Linux 无 GUI 部署形态。
#
# 只含网关：不带 Magic Proxy 隧道/抓包/macOS GUI 依赖（rumps/pyobjc/
# mitmproxy 均不装）。sysctl/ 与 capture/ 仅因 mpconf.config 的模块级
# import 链被带入（keychain 的 Security 由 entry.py 注入 stub，见
# docs/docker-deploy.md「Linux 兼容」）。
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 依赖层单独先行——代码变动不失效缓存
COPY requirements-suanpan.txt /app/requirements-suanpan.txt
RUN pip install --no-cache-dir -r /app/requirements-suanpan.txt

COPY suanpan/ /app/suanpan/
COPY mpconf/ /app/mpconf/
COPY services/ /app/services/
COPY sysctl/ /app/sysctl/
COPY capture/ /app/capture/
COPY docker/entry.py /app/docker/entry.py

# 容器内监听端口（config listen_port 可改；compose 映射须同步改）
EXPOSE 9527

CMD ["python3", "/app/docker/entry.py", "serve"]
