import unittest
import os
import tempfile
import time

from supysonic import db
from supysonic.emo.ws_store import (
    getLocalQueueState,
    getPlaybackState,
    getPlaybackStates,
    getQueueState,
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
