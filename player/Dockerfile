# Independent player image. Never use the server image name for this target.
FROM python:3.12.16-alpine3.23
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="EmoSonic headless player" \
      org.opencontainers.image.revision=$SOURCE_REVISION
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/var/lib/emosonic-player XDG_RUNTIME_DIR=/run/emosonic-player
# Pins are from the official Alpine v3.23 aarch64 package repository.
RUN apk add --no-cache mpv=0.40.0-r8 tini=0.19.0-r3 \
    && addgroup -g 10001 player \
    && adduser -D -u 10001 -G player -h /var/lib/emosonic-player player \
    && mkdir -p /run/emosonic-player /var/lib/emosonic-player /opt/player \
    && chown -R 10001:10001 /run/emosonic-player /var/lib/emosonic-player \
    && chmod 0700 /run/emosonic-player /var/lib/emosonic-player
WORKDIR /opt/player
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \
    && python -m pip check \
    && mpv --version \
    && mpv --ao=help | grep -q alsa \
    && python -m pip freeze > /opt/player/python-packages.txt \
    && apk info -v > /opt/player/system-packages.txt
USER 10001:10001
ENTRYPOINT ["/sbin/tini", "--", "emosonic-playerd"]
CMD ["--config", "/etc/emosonic-player/config.json"]
