"""Owned-process lifecycle; no SSH, host configuration or deployment actions."""
import argparse
import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import uuid

from .config import client_identity, load_config, read_password
from .mpv import MpvPlayer
from .playback import Player, PlaybackError
from .protocol import ProtocolClient, ProtocolError
from .stream import MediaClient, MediaError

logger = logging.getLogger(__name__)


def private_directory(path):
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('runtime directory must be owned by this user and mode 0700')
    return directory


async def serve_cli(reader, writer, player):
    try:
        peer = writer.get_extra_info('socket')
        _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            raise ValueError('different user')
        async with asyncio.timeout(40):
            line = await reader.readline()
            if len(line) > 16384:
                raise ValueError('request too large')
            request = json.loads(line)
            if set(request) != {'action', 'params'} or not isinstance(request['params'], dict):
                raise ValueError('invalid request')
            result = await player.cli(request['action'], request['params'])
        response = {'ok': True, 'result': result}
    except asyncio.CancelledError:
        writer.close()
        raise
    except (PlaybackError, ProtocolError, MediaError) as exc:
        # These exception classes contain authored categories, not raw server text.
        response = {'ok': False, 'error': str(exc)}
    except Exception:
        response = {'ok': False, 'error': 'request_failed'}
    try:
        writer.write((json.dumps(response) + '\n').encode())
        await writer.drain()
    except (OSError, ConnectionError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def run(config):
    runtime = private_directory(config['runtime_dir'])
    lockfd = os.open(runtime / 'daemon.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lockfd)
        raise ValueError('another player daemon is running') from None
    password = read_password(config['password_file'])
    identity = client_identity(config['client_id_file'])
    wire = ProtocolClient(config['server_url'], config['username'], password, identity,
                          'instance-' + uuid.uuid4().hex, config['device_name'])
    media = MediaClient(config['server_url'], config['username'], password)
    engine = MpvPlayer(config['audio_device'], str(runtime))
    player = Player(wire, engine, media)
    wire.on_event = player.on_event
    wire.on_disconnect = player.disconnected
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    path = runtime / 'control.sock'
    server = None
    try:
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(path.lstat().st_mode) or path.lstat().st_uid != os.getuid():
                raise ValueError('unsafe existing control socket path')
            path.unlink()
        await engine.start()
        await engine.set_volume(config['initial_volume'])
        await player.start()
        server = await asyncio.start_unix_server(
            lambda r, w: serve_cli(r, w, player), path=path, limit=16385)
        os.chmod(path, 0o600)
        delay = 1
        while not stop.is_set():
            try:
                if player.needs_engine_restart:
                    await engine.close()
                    await engine.start()
                    await engine.set_volume(config['initial_volume'])
                    player.needs_engine_restart = False
                await wire.connect()
                await player.reconcile()
                delay = 1
                logger.info('player registered and reconciled')
                while wire.ready and not stop.is_set() and not player.needs_engine_restart:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('player connection or reconciliation failed')
            await player.disconnected()
            await wire.close()
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(30, delay * 2)
    finally:
        if server:
            server.close()
            await server.wait_closed()
        await wire.close()
        await player.close()
        await media.close()
        if path.exists():
            path.unlink()
        os.close(lockfd)


def main():
    parser = argparse.ArgumentParser(description='EmoSonic Core-only audio daemon')
    parser.add_argument('--config', default='/etc/emosonic-player/config.json')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    for name in ('socketio', 'engineio', 'aiohttp'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(run(load_config(args.config)))
    except (ValueError, OSError):
        logger.error('daemon startup failed; check configuration and device permissions')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
