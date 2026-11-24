import asyncio
import json
import unittest
from unittest.mock import MagicMock

import aiohttp

from emosonic_player.stream import MAX_RESPONSE_BYTES, MediaClient, MediaError


class MediaResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.content = aiohttp.StreamReader(MagicMock(), limit=64 * 1024)
        self.api = MediaClient('https://example.test', 'user', 'test-password')
        self.api.session = MagicMock()
        response = self.api.session.get.return_value.__aenter__.return_value
        response.status = 200
        response.content = self.content

    async def test_playlist_handles_short_first_read_at_size_limit(self):
        body = json.dumps({'subsonic-response': {
            'status': 'ok', 'playlist': {'entry': [{'id': 'one'}, {'id': 'two'}]}
        }}).encode()
        self.content.feed_data(body[:24])
        request = asyncio.create_task(self.api.playlist('list'))
        await asyncio.sleep(0)
        self.content.feed_data(body[24:] + b' ' * (MAX_RESPONSE_BYTES - len(body)))
        self.content.feed_eof()
        self.assertEqual(await request, ['one', 'two'])

    async def test_limit_applies_across_chunks_before_eof(self):
        self.content.feed_data(b' ' * MAX_RESPONSE_BYTES)
        request = asyncio.create_task(self.api.playlist('list'))
        await asyncio.sleep(0)
        self.content.feed_data(b' ')
        with self.assertRaisesRegex(MediaError, 'response too large'):
            await asyncio.wait_for(request, 1)
