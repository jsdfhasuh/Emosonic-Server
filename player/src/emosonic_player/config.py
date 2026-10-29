"""Fail-closed configuration and persistent, non-secret identity."""
import json
import os
from pathlib import Path
import stat
import uuid
from urllib.parse import urlsplit


def load_config(path: str) -> dict:
    config = json.loads(Path(path).read_text())
    for key in ('server_url', 'username', 'password_file', 'audio_device'):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError('missing configuration: ' + key)
    url = urlsplit(config['server_url'])
    if url.username or url.password or url.query or url.fragment:
        raise ValueError('server_url must not contain credentials, query or fragment')
    if url.scheme != 'https' and not (
        config.get('allow_insecure_http') is True and url.scheme == 'http'
    ):
        raise ValueError('HTTPS required; HTTP is explicit local development only')
    if not url.hostname:
        raise ValueError('server hostname required')
    if not config['audio_device'].startswith('alsa/') or config['audio_device'] in ('alsa/null', 'alsa/default'):
        raise ValueError('an explicit enumerated ALSA device is required')
    config.setdefault('device_name', 'OpenWrt speaker')
    config.setdefault('runtime_dir', '/run/emosonic-player')
    config.setdefault('state_dir', '/var/lib/emosonic-player')
    config.setdefault('client_id_file', str(Path(config['state_dir']) / 'client-id'))
    config.setdefault('initial_volume', 20)
    if type(config['initial_volume']) not in (int, float) or not 0 <= config['initial_volume'] <= 100:
        raise ValueError('initial_volume must be between 0 and 100')
    return config


def read_password(path: str) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('credential file must be regular and owner-only')
        with os.fdopen(fd, 'r', closefd=False) as stream:
            value = stream.read(8193)
        if len(value) > 8192 or not value.rstrip('\r\n'):
            raise ValueError('invalid credential file')
        return value.rstrip('\r\n')
    finally:
        os.close(fd)


def client_identity(path: str) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = 'openwrt-' + uuid.uuid4().hex
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        value = target.read_text().strip()
        if not value or len(value) > 128:
            raise ValueError('invalid saved client identity')
        return value
    with os.fdopen(fd, 'w') as stream:
        stream.write(value + '\n')
    return value
