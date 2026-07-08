import unittest
import os
import tempfile
import time

from supysonic import db
from supysonic.emo.ws_store import (
    getDevicePlaybackState,
    getPlaybackContextState,
    getPlaybackHandoff,
    getLocalQueueState,
    getPlaybackState,
    getPlaybackStates,
    getQueueState,
    saveDevicePlaybackState,
    savePlaybackContextState,
    savePlaybackHandoff,
    saveLocalQueueState,
    savePlaybackState,
    saveQueueState,
)


class EmoWebSocketStoreTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp()
        os.close(handle)
        db.init_database("sqlite:///" + self.db_path)

    def tearDown(self):
        db.release_database()
        os.remove(self.db_path)

    def test_save_and_load_queue_state(self):
        saveQueueState(
            "root:living-room",
            "root",
            "player-1",
            ["songId1", "songId2"],
            1,
            4200,
        )

        queue_state = getQueueState("root:living-room")
        self.assertEqual(queue_state["sessionId"], "root:living-room")
        self.assertEqual(queue_state["currentIndex"], 1)
        self.assertEqual(queue_state["positionMs"], 4200)
        self.assertEqual(queue_state["queueSongIds"][0], "songId1")
        self.assertEqual(queue_state["queueRevision"], 1)
        self.assertIn("serverUpdatedAtMs", queue_state)

    def test_save_and_load_playback_state(self):
        savePlaybackState(
            "root:living-room",
            "root",
            "player-1",
            {
                "sessionId": "root:living-room",
                "state": "playing",
                "trackId": "track-1",
                "positionMs": 4200,
                "volume": 65,
            },
        )

        playback_state = getPlaybackState("root:living-room", "player-1")
        self.assertEqual(playback_state["sessionId"], "root:living-room")
        self.assertEqual(playback_state["sourceClientId"], "player-1")
        self.assertEqual(playback_state["state"], "playing")
        self.assertEqual(playback_state["trackId"], "track-1")
        self.assertEqual(playback_state["positionMs"], 4200)
        self.assertEqual(playback_state["volume"], 65)
        self.assertIn("serverUpdatedAtMs", playback_state)
        self.assertNotIn("serverTimeMs", playback_state)

        all_states = getPlaybackStates("root:living-room")
        self.assertEqual(len(all_states), 1)

    def test_load_playback_state_strips_expired_effective_at(self):
        savePlaybackState(
            "root:living-room",
            "root",
            "player-1",
            {
                "sessionId": "root:living-room",
                "state": "playing",
                "trackId": "track-1",
                "positionMs": 4200,
                "effectiveAtServerMs": int(time.time() * 1000) - 1000,
            },
        )

        playback_state = getPlaybackState("root:living-room", "player-1")
        self.assertNotIn("effectiveAtServerMs", playback_state)

    def test_save_and_load_playback_context_state(self):
        savePlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "phone-1",
                "originClientId": "phone-1",
                "queueSongIds": ["song-1", "song-2"],
                "currentIndex": 1,
                "trackId": "song-2",
                "state": "playing",
                "positionMs": 4200,
                "queueRevision": 2,
                "controlVersion": 3,
                "version": 4,
                "epoch": 2,
            },
        )

        context = getPlaybackContextState("playback:alice:main")
        self.assertEqual(context["playbackContextId"], "playback:alice:main")
        self.assertEqual(context["authorityClientId"], "phone-1")
        self.assertEqual(context["queueSongIds"], ["song-1", "song-2"])
        self.assertEqual(context["currentIndex"], 1)
        self.assertEqual(context["controlVersion"], 3)
        self.assertTrue(context["authoritative"])

    def test_save_and_load_device_playback_state(self):
        saveDevicePlaybackState(
            "playback:alice:main",
            "root:pc",
            "alice",
            "pc-1",
            {
                "playbackContextId": "playback:alice:main",
                "deviceSessionId": "root:pc",
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 999,
            },
            is_authority=False,
            mode="handoff",
        )

        feedback = getDevicePlaybackState("playback:alice:main", "pc-1")
        self.assertEqual(feedback["deviceSessionId"], "root:pc")
        self.assertEqual(feedback["positionMs"], 999)
        self.assertFalse(feedback["isAuthority"])
        self.assertEqual(feedback["mode"], "handoff")

    def test_save_and_load_playback_handoff(self):
        savePlaybackHandoff(
            {
                "handoffId": "handoff-1",
                "requestId": "request-1",
                "playbackContextId": "playback:alice:main",
                "userName": "alice",
                "sourceClientId": "phone-1",
                "targetClientId": "pc-1",
                "originClientId": "phone-1",
                "status": "preparing",
                "baseControlVersion": 3,
                "controlVersion": 4,
                "snapshot": {"trackId": "song-1"},
            },
        )

        handoff = getPlaybackHandoff("handoff-1")
        self.assertEqual(handoff["playbackContextId"], "playback:alice:main")
        self.assertEqual(handoff["status"], "preparing")
        self.assertEqual(handoff["baseControlVersion"], 3)
        self.assertEqual(handoff["controlVersion"], 4)
        self.assertEqual(handoff["snapshot"]["trackId"], "song-1")

    def test_save_and_load_local_queue_state(self):
        saveLocalQueueState(
            "root:living-room",
            "player-1",
            ["songId3", "songId4"],
            1,
            0,
        )

        queue_state = getLocalQueueState("root:living-room", "player-1")
        self.assertEqual(queue_state["sourceClientId"], "player-1")
        self.assertEqual(queue_state["currentIndex"], 1)
        self.assertEqual(queue_state["queueSongIds"][1], "songId4")
        self.assertIn("serverUpdatedAtMs", queue_state)
