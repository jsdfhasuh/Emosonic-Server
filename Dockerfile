# 使用 Python 3.11 Alpine 作为基础镜像 (更稳定的选择)
FROM python:3.11-alpine

ARG SERVER_BUILD_COMMIT=unknown
ENV EMO_SERVER_BUILD_COMMIT=$SERVER_BUILD_COMMIT
LABEL org.opencontainers.image.revision=$SERVER_BUILD_COMMIT

# 设置工作目录
WORKDIR /app

# 预创建日志目录，方便非挂载场景直接运行
RUN mkdir -p /log/web /log/daemon

# 安装系统依赖
RUN apk add --no-cache \
    # 音频处理相关
    ffmpeg \
    # 可选的转码工具
    lame \
    flac \
    # 时区支持
    tzdata \
    # SQLite 支持
    sqlite \
    # 图像处理库依赖
    jpeg-dev \
    zlib-dev \
    # 如果需要 MySQL/MariaDB 支持
    mariadb-connector-c-dev 


# 设置时区为北京时间
RUN cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone

# 拷贝项目文件
COPY . /app/

# 安装 Python 依赖和项目本身（生成 supysonic-server 等入口命令）
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir . && \
    # 清理缓存
    find /app -type d -name __pycache__ -exec rm -rf {} +

# 暴露端口
EXPOSE 5000

# 启动命令（setup.sh 内部会使用 supysonic-server -S gevent）
ENTRYPOINT ["sh", "/app/setup.sh"]
