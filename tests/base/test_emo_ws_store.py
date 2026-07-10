import unittest
import os
import tempfile
import time

from supysonic import db
from supysonic.emo.ws_store import (
    createPlaybackContextState,
    deletePlaybackContext,
    expirePlaybackContext,
    getActivePlaybackHandoffs,
    getDevicePlaybackState,
    getDevicePlaybackStates,
    getPlaybackContextState,
    getPlaybackContextWithDeviceStates,
    getPlaybackHandoff,
    getPlaybackHandoffByRequest,
    getLocalQueueState,
    getPlaybackState,
    getPlaybackStates,
    getQueueState,
    listUserPlaybackContexts,
    saveDevicePlaybackState,
    savePlaybackContextState,
    savePlaybackHandoff,
    saveLocalQueueState,
    savePlaybackState,
    saveQueueState,
    serializeDevicePlaybackStateV2,
    serializePlaybackContextV2,
    updatePlaybackContextState,
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

    def test_save_playback_context_state_preserves_zero_counters(self):
        savePlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "phone-1",
                "originClientId": "phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "trackId": "song-1",
                "state": "stopped",
                "positionMs": 0,
                "queueRevision": 0,
                "controlVersion": 0,
                "version": 0,
                "epoch": 0,
            },
        )

        created_context = getPlaybackContextState("playback:alice:main")
        for counter_name in ("queueRevision", "controlVersion", "version", "epoch"):
            self.assertEqual(created_context[counter_name], 0)

        updatePlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "phone-1",
                "originClientId": "phone-1",
                "queueSongIds": ["song-1", "song-2"],
                "currentIndex": 1,
                "trackId": "song-2",
                "state": "paused",
                "positionMs": 1200,
                "queueRevision": 0,
                "controlVersion": 0,
                "version": 0,
                "epoch": 0,
            },
        )

        updated_context = getPlaybackContextState("playback:alice:main")
        self.assertEqual(updated_context["state"], "paused")
        self.assertEqual(updated_context["trackId"], "song-2")
        for counter_name in ("queueRevision", "controlVersion", "version", "epoch"):
            self.assertEqual(updated_context[counter_name], 0)

    def test_create_playback_context_state_does_not_overwrite_existing(self):
        created = createPlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "phone-1",
                "originClientId": "phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "trackId": "song-1",
                "state": "stopped",
                "positionMs": 0,
                "controlVersion": 1,
            },
        )
        duplicate = createPlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "pc-1",
                "originClientId": "pc-1",
                "queueSongIds": ["song-2"],
                "currentIndex": 0,
                "trackId": "song-2",
                "state": "playing",
                "positionMs": 100,
                "controlVersion": 2,
            },
        )

        context = getPlaybackContextState("playback:alice:main")
        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(context["authorityClientId"], "phone-1")
        self.assertEqual(context["trackId"], "song-1")
        self.assertEqual(context["controlVersion"], 1)

    def test_update_playback_context_state_requires_existing_record(self):
        missing = updatePlaybackContextState(
            "playback:alice:missing",
            "alice",
            {
                "playbackContextId": "playback:alice:missing",
                "authorityClientId": "phone-1",
                "originClientId": "phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "trackId": "song-1",
                "state": "playing",
                "positionMs": 100,
                "controlVersion": 1,
            },
        )
        createPlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "phone-1",
                "originClientId": "phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "trackId": "song-1",
                "state": "stopped",
                "positionMs": 0,
                "controlVersion": 1,
            },
        )
        updated = updatePlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "phone-1",
                "originClientId": "controller-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "trackId": "song-1",
                "state": "playing",
                "positionMs": 500,
                "controlVersion": 2,
            },
        )

        self.assertFalse(missing)
        self.assertIsNone(getPlaybackContextState("playback:alice:missing"))
        self.assertTrue(updated)
        context = getPlaybackContextState("playback:alice:main")
        self.assertEqual(context["state"], "playing")
        self.assertEqual(context["originClientId"], "controller-1")
        self.assertEqual(context["positionMs"], 500)
        self.assertEqual(context["controlVersion"], 2)

    def test_playback_context_update_rejects_cross_user_overwrite(self):
        createPlaybackContextState(
            "playback:shared",
            "alice",
            {
                "playbackContextId": "playback:shared",
                "authorityClientId": "alice-phone",
                "queueSongIds": ["song-1"],
            },
        )

        with self.assertRaises(PermissionError):
            savePlaybackContextState(
                "playback:shared",
                "bob",
                {
                    "playbackContextId": "playback:shared",
                    "authorityClientId": "bob-phone",
                    "queueSongIds": ["song-2"],
                },
            )

        context = getPlaybackContextState("playback:shared")
        self.assertEqual(context["userName"], "alice")
        self.assertEqual(context["authorityClientId"], "alice-phone")
        self.assertEqual(context["queueSongIds"], ["song-1"])

    def test_get_playback_context_with_device_states(self):
        createPlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "phone-1",
                "originClientId": "phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "trackId": "song-1",
                "state": "playing",
                "positionMs": 100,
                "controlVersion": 1,
            },
        )
        saveDevicePlaybackState(
            "playback:alice:main",
            "root:phone",
            "alice",
            "phone-1",
            {
                "playbackContextId": "playback:alice:main",
                "deviceSessionId": "root:phone",
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 100,
            },
            is_authority=True,
        )

        status = getPlaybackContextWithDeviceStates("playback:alice:main")

        self.assertEqual(status["playbackContext"]["playbackContextId"], "playback:alice:main")
        self.assertEqual(len(status["deviceStates"]), 1)
        self.assertEqual(status["deviceStates"][0]["sourceClientId"], "phone-1")
        self.assertIsNone(getPlaybackContextWithDeviceStates("playback:alice:missing"))

    def test_list_user_playback_contexts_filters_by_user(self):
        createPlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "phone-1",
                "originClientId": "phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "trackId": "song-1",
                "state": "playing",
                "positionMs": 100,
            },
        )
        createPlaybackContextState(
            "playback:bob:main",
            "bob",
            {
                "playbackContextId": "playback:bob:main",
                "authorityClientId": "bob-phone",
                "originClientId": "bob-phone",
                "queueSongIds": ["song-2"],
                "currentIndex": 0,
                "trackId": "song-2",
                "state": "playing",
                "positionMs": 200,
            },
        )

        contexts = listUserPlaybackContexts("alice")

        self.assertEqual([context["playbackContextId"] for context in contexts], ["playback:alice:main"])

    def test_delete_playback_context_removes_device_states(self):
        createPlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "phone-1",
                "originClientId": "phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "trackId": "song-1",
                "state": "playing",
                "positionMs": 100,
            },
        )
        saveDevicePlaybackState(
            "playback:alice:main",
            "root:phone",
            "alice",
            "phone-1",
            {
                "playbackContextId": "playback:alice:main",
                "deviceSessionId": "root:phone",
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 100,
            },
        )

        self.assertTrue(deletePlaybackContext("playback:alice:main"))
        self.assertFalse(deletePlaybackContext("playback:alice:main"))
        self.assertIsNone(getPlaybackContextState("playback:alice:main"))
        self.assertEqual(getDevicePlaybackStates("playback:alice:main"), [])

    def test_expire_playback_context_requires_existing_record(self):
        self.assertIsNone(expirePlaybackContext("playback:alice:missing"))
        createPlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "phone-1",
                "originClientId": "phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "trackId": "song-1",
                "state": "playing",
                "positionMs": 100,
                "version": 2,
            },
        )

        expired = expirePlaybackContext("playback:alice:main")

        self.assertEqual(expired["state"], "expired")
        self.assertEqual(expired["version"], 3)
        self.assertEqual(getPlaybackContextState("playback:alice:main")["state"], "expired")

    def test_serialize_playback_context_v2_strips_legacy_aliases(self):
        savePlaybackContextState(
            "playback:alice:main",
            "alice",
            {
                "playbackContextId": "playback:alice:main",
                "authorityClientId": "phone-1",
                "originClientId": "phone-1",
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "trackId": "song-1",
                "state": "playing",
                "positionMs": 4200,
                "volume": 40,
            },
        )

        legacy_context = getPlaybackContextState("playback:alice:main")
        v2_context = serializePlaybackContextV2(legacy_context)

        self.assertEqual(legacy_context["sessionId"], "playback:alice:main")
        self.assertEqual(legacy_context["sourceClientId"], "phone-1")
        self.assertEqual(legacy_context["volume"], 40)
        self.assertNotIn("sessionId", v2_context)
        self.assertNotIn("sourceClientId", v2_context)
        self.assertNotIn("volume", v2_context)
        self.assertEqual(v2_context["logicalVolume"], 40)
        self.assertEqual(v2_context["playbackContextId"], "playback:alice:main")
        self.assertEqual(v2_context["authorityClientId"], "phone-1")

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
                "muted": True,
                "outputDeviceId": "dac-1",
                "audioDeviceName": "USB DAC",
            },
            is_authority=False,
            mode="handoff",
        )

        feedback = getDevicePlaybackState("playback:alice:main", "pc-1")
        self.assertEqual(feedback["deviceSessionId"], "root:pc")
        self.assertEqual(feedback["positionMs"], 999)
        self.assertTrue(feedback["muted"])
        self.assertEqual(feedback["outputDeviceId"], "dac-1")
        self.assertEqual(feedback["audioDeviceName"], "USB DAC")
        self.assertFalse(feedback["isAuthority"])
        self.assertEqual(feedback["mode"], "handoff")

    def test_serialize_device_playback_state_v2_strips_legacy_aliases(self):
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
                "muted": True,
                "outputDeviceId": "dac-1",
                "audioDeviceName": "USB DAC",
            },
            is_authority=False,
            mode="handoff",
        )

        legacy_feedback = getDevicePlaybackState("playback:alice:main", "pc-1")
        v2_feedback = serializeDevicePlaybackStateV2(legacy_feedback)

        self.assertEqual(legacy_feedback["sessionId"], "root:pc")
        self.assertEqual(legacy_feedback["sourceClientId"], "pc-1")
        self.assertNotIn("sessionId", v2_feedback)
        self.assertNotIn("sourceClientId", v2_feedback)
        self.assertEqual(v2_feedback["clientId"], "pc-1")
        self.assertEqual(v2_feedback["deviceSessionId"], "root:pc")
        self.assertTrue(v2_feedback["muted"])
        self.assertEqual(v2_feedback["outputDeviceId"], "dac-1")
        self.assertEqual(v2_feedback["audioDeviceName"], "USB DAC")

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

    def test_get_playback_handoff_by_request_is_scoped_by_origin_client(self):
        savePlaybackHandoff(
            {
                "handoffId": "handoff-controller-1",
                "requestId": "request-1",
                "playbackContextId": "playback:alice:main",
                "userName": "alice",
                "sourceClientId": "phone-1",
                "targetClientId": "pc-1",
                "originClientId": "controller-1",
                "status": "preparing",
                "baseControlVersion": 3,
                "controlVersion": 4,
                "snapshot": {"trackId": "song-1"},
            },
        )
        savePlaybackHandoff(
            {
                "handoffId": "handoff-controller-2",
                "requestId": "request-1",
                "playbackContextId": "playback:alice:other",
                "userName": "alice",
                "sourceClientId": "tablet-1",
                "targetClientId": "speaker-1",
                "originClientId": "controller-2",
                "status": "preparing",
                "baseControlVersion": 5,
                "controlVersion": 6,
                "snapshot": {"trackId": "song-2"},
            },
        )

        first = getPlaybackHandoffByRequest("alice", "controller-1", "request-1")
        second = getPlaybackHandoffByRequest("alice", "controller-2", "request-1")

        self.assertEqual(first["handoffId"], "handoff-controller-1")
        self.assertEqual(second["handoffId"], "handoff-controller-2")

    def test_get_active_playback_handoffs_filters_terminal_states(self):
        for handoff_id, status in (
            ("handoff-preparing", "preparing"),
            ("handoff-ready", "ready"),
            ("handoff-completed", "completed"),
        ):
            savePlaybackHandoff(
                {
                    "handoffId": handoff_id,
                    "requestId": handoff_id,
                    "playbackContextId": "playback:alice:main",
                    "userName": "alice",
                    "sourceClientId": "phone-1",
                    "targetClientId": "pc-1",
                    "originClientId": "phone-1",
                    "status": status,
                    "baseControlVersion": 3,
                    "controlVersion": 4,
                    "snapshot": {"trackId": "song-1"},
                }
            )

        active = getActivePlaybackHandoffs("playback:alice:main")

        self.assertEqual(
            {handoff["handoffId"] for handoff in active},
            {"handoff-preparing", "handoff-ready"},
        )

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
