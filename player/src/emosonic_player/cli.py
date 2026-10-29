"""User CLI: only the daemon speaks to the engine and canonical server."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys


def parser():
    root = argparse.ArgumentParser(description='EmoSonic headless player control')
    root.add_argument('--socket', default='/run/emosonic-player/control.sock')
    commands = root.add_subparsers(dest='action', required=True)
    for command in ('status', 'queue', 'pause', 'resume', 'next', 'prev', 'stop', 'playlists'):
        commands.add_parser(command)
    search = commands.add_parser('search')
    search.add_argument('query')
    play = commands.add_parser('play')
    play.add_argument('song', nargs='?')
    choice = play.add_mutually_exclusive_group()
    choice.add_argument('--playlist')
    choice.add_argument('--album')
    seek = commands.add_parser('seek')
    seek.add_argument('seconds', help='absolute seconds, or signed relative offset')
    volume = commands.add_parser('volume')
    volume.add_argument('value', type=float)
    return root


async def call(socket_path, action, params):
    async with asyncio.timeout(45):
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=1024 * 1024)
        try:
            writer.write((json.dumps({'action': action, 'params': params}) + '\n').encode())
            await writer.drain()
            line = await reader.readline()
            if not line:
                raise ValueError('empty response')
            response = json.loads(line)
            if not response.get('ok'):
                raise ValueError(response.get('error', 'request failed'))
            return response['result']
        finally:
            writer.close()
            await writer.wait_closed()


def main():
    args = vars(parser().parse_args())
    socket_path, action = args.pop('socket'), args.pop('action')
    params = {k: v for k, v in args.items() if v is not None}
    if action == 'play' and sum(k in params for k in ('song', 'playlist', 'album')) != 1:
        parser().error('play requires one song ID, --playlist or --album')
    try:
        result = asyncio.run(call(socket_path, action, params))
    except ValueError as exc:
        print('Request rejected: ' + str(exc)[:160], file=sys.stderr)
        return 1
    except (OSError, TimeoutError):
        print('Request failed; check daemon status and non-sensitive logs.', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
