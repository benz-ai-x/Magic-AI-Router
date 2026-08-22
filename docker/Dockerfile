# Suanpan 网关 Docker 镜像（issue #22）——Linux 无 GUI 部署形态。
#
# 只含网关：不带 Magic Proxy 隧道/抓包/macOS GUI 依赖（rumps/pyobjc/
# mitmproxy 均不装）。sysctl/ 与 capture/ 仅因 mpconf.config 的模块级
# import 链被带入（keychain 的 Security 为可选导入，Linux 下为 None，
# 见 docs/docker-deploy.md「Linux 兼容」）。
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
COPY tunnel/ /app/tunnel/
# config-ui（:9528 配置页面）的页面资源与 util.resource_path 查找目标
COPY shellui/ /app/shellui/
COPY util.py /app/util.py
COPY docs/agent.md /app/docs/agent.md
COPY docker/entry.py /app/docker/entry.py

# 容器内监听端口（config listen_port 可改；compose 映射须同步改）
EXPOSE 9527
EXPOSE 9528

# 默认同时跑网关(:9527) + 配置页面(:9528)；entry.py 内部并发两个服务
CMD ["python3", "/app/docker/entry.py", "serve"]
