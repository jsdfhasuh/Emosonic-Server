"""Subsonic HTTPS API; signed URLs are private values, never command arguments."""
import hashlib
import secrets
from urllib.parse import urlencode

import aiohttp


class MediaError(RuntimeError):
    pass


class MediaClient:
    def __init__(self, server_url: str, username: str, password: str):
        self.base = server_url.rstrip('/')
        self.username = username
        self._password = password
        self.session = None

    def _auth(self):
        salt = secrets.token_hex(16)
        return {'u': self.username, 't': hashlib.md5((self._password + salt).encode()).hexdigest(),
                's': salt, 'v': '1.16.1', 'c': 'emosonic-player', 'f': 'json'}

    def stream_url(self, song_id: str) -> str:
        return self.base + '/rest/stream.view?' + urlencode({**self._auth(), 'id': song_id})

    async def get(self, endpoint: str, **params) -> dict:
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        try:
            async with self.session.get(self.base + '/rest/' + endpoint + '.view',
                                        params={**self._auth(), **params}, allow_redirects=False) as response:
                if response.status != 200:
                    raise MediaError('music API request failed')
                raw = await response.content.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise MediaError('music API response too large')
                import json
                result = json.loads(raw).get('subsonic-response', {})
                if result.get('status') != 'ok':
                    raise MediaError('music API rejected request')
                return result
        except (aiohttp.ClientError, ValueError, TimeoutError) as exc:
            raise MediaError('music API unavailable') from None

    async def search(self, query):
        result = await self.get('search3', query=query, songCount=50, artistCount=0, albumCount=0)
        return [{k: song[k] for k in ('id', 'title', 'artist', 'album', 'duration') if k in song}
                for song in result.get('searchResult3', {}).get('song', [])]

    async def playlist(self, playlist_id):
        result = await self.get('getPlaylist', id=playlist_id)
        return [song['id'] for song in result.get('playlist', {}).get('entry', [])]

    async def album(self, album_id):
        result = await self.get('getAlbum', id=album_id)
        return [song['id'] for song in result.get('album', {}).get('song', [])]

    async def playlists(self):
        result = await self.get('getPlaylists')
        return [{k: item[k] for k in ('id', 'name', 'songCount') if k in item}
                for item in result.get('playlists', {}).get('playlist', [])]

    async def close(self):
        if self.session:
            await self.session.close()
