import os

from supysonic.db import Album, Artist, Folder, Track

from .frontendtestbase import FrontendTestBase


class PlayerTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Player Artist")
        self.album = Album.create(name="Player Album", artist=self.artist)
        self.track = self._create_track("Socket Song", 1)
        self.other_track = self._create_track("Another Track", 2)

    def _create_track(self, title, number, artist=None, album=None):
        artist = artist or self.artist
        album = album or self.album
        return Track.create(
            disc=1,
            number=number,
            title=title,
            duration=180 + number,
            has_art=False,
            album=album,
            artist=artist,
            genre="rock",
            bitrate=320,
            path=os.path.join("/music", f"{number}.flac"),
            last_modification=1,
            root_folder=self.root,
            folder=self.root,
        )

    def test_player_page_exposes_socket_player_contract(self):
        self._login("alice", "Alic3")

        rv = self.client.get("/player")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Web player", rv.data)
        self.assertIn("data-player-search-url=\"/player/search\"", rv.data)
        self.assertIn("data-player-meta-url=\"/player/track-meta\"", rv.data)
        self.assertIn("window.location.origin + '/emo'", rv.data)
        self.assertIn("path: '/emo/ws'", rv.data)
        self.assertIn("'device.register'", rv.data)
        self.assertIn("roles: ['player']", rv.data)
        self.assertIn("'queue.local.get'", rv.data)
        self.assertIn("payload.sourceClientId || payload.clientId", rv.data)
        self.assertIn("ownerClientId !== playerState.selfClientId", rv.data)
        self.assertIn("normalizeQueueIndex", rv.data)
        self.assertIn("playerState.activeQueueType === 'session'", rv.data)
        self.assertIn("!playerState.broadcast.active", rv.data)
        self.assertIn("applyPlaybackUpdate", rv.data)
        self.assertIn("initialReportTimer", rv.data)
        self.assertIn("stateName === 'playing' && playerState.userGestureSeen", rv.data)
        self.assertIn("playQueueItem(queueType, index, positionMs, shouldAutoplay, null, false)", rv.data)
        self.assertIn("playerAudio.addEventListener('ended', () =>", rv.data)
        self.assertIn("coverRequestToken", rv.data)
        self.assertIn("coverFailed", rv.data)
        self.assertIn("new Image()", rv.data)
        self.assertIn("removeAttribute('src')", rv.data)
        self.assertIn('id="player-dock-title"', rv.data)
        self.assertNotIn('id="player-dock-title" data-i18n', rv.data)
        self.assertIn("player.setVolume", rv.data)
        self.assertIn("broadcast.start", rv.data)
        self.assertIn("broadcastId", rv.data)
        self.assertIn("emosonic.webPlayer.clientId", rv.data)
        self.assertIn("emosonic.webPlayer.sessionId", rv.data)

    def test_player_search_returns_track_payloads_for_title_artist_and_album(self):
        self._login("alice", "Alic3")

        by_title = self.client.get("/player/search?q=Socket&limit=10")
        by_artist = self.client.get("/player/search?q=Player%20Artist&limit=10")
        by_album = self.client.get("/player/search?q=Player%20Album&limit=10")

        self.assertEqual(by_title.status_code, 200)
        self.assertEqual(by_title.json["tracks"][0]["title"], "Socket Song")
        self.assertEqual(by_title.json["tracks"][0]["artist"], "Player Artist")
        self.assertEqual(by_title.json["tracks"][0]["album"], "Player Album")
        self.assertIn("/player/cover/", by_title.json["tracks"][0]["coverUrl"])
        self.assertIn("/rest/stream.view?id=", by_title.json["tracks"][0]["streamUrl"])
        self.assertIn("c=web-player", by_title.json["tracks"][0]["streamUrl"])
        self.assertTrue(
            any(track["id"] == str(self.track.id) for track in by_artist.json["tracks"])
        )
        self.assertTrue(
            any(track["id"] == str(self.track.id) for track in by_album.json["tracks"])
        )

    def test_player_track_meta_returns_requested_tracks_and_ignores_bad_ids(self):
        self._login("alice", "Alic3")

        rv = self.client.get(f"/player/track-meta?ids={self.track.id}&ids=not-a-uuid")

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(set(rv.json.keys()), {str(self.track.id)})
        self.assertEqual(rv.json[str(self.track.id)]["title"], "Socket Song")
        self.assertIn("/rest/stream.view?id=", rv.json[str(self.track.id)]["streamUrl"])

    def test_player_requires_login(self):
        rv = self.client.get("/player", follow_redirects=True)

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Please login", rv.data)
