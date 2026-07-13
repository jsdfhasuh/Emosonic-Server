import concurrent.futures
import os
import tempfile
import threading
import time
import unittest

from supysonic import db
from supysonic.emo.ws_store import (
    PlaybackContextClosedError,
    PlaybackContextIntentConflictError,
    PlaybackContextStaleVersionError,
    closeStrictPlaybackContextState,
    completeStrictPlaybackHandoff,
    createPlaybackContextState,
    createStrictPlaybackContextState,
    deletePlaybackContext,
    expirePlaybackContext,
    failActivePlaybackHandoffsForRestart,
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
    listPlaybackContexts,
    mutateStrictPlaybackContextControl,
    saveDevicePlaybackState,
    savePlaybackContextState,
    savePlaybackHandoff,
    saveLocalQueueState,
    savePlaybackState,
    saveQueueState,
    serializeDevicePlaybackStateV2,
    serializePlaybackContextV2,
    terminateStrictPlaybackHandoff,
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

    def test_strict_context_mutations_serialize_same_base_cursor(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            0,
            "playing",
        )
        barrier = threading.Barrier(2)

        def mutate(action):
            barrier.wait()
            try:
                mutateStrictPlaybackContextControl(
                    "context-1",
                    "alice",
                    "controller-1",
                    action,
                    1,
                    position_ms=100 if action == "player.seek" else None,
                )
                return "accepted"
            except PlaybackContextStaleVersionError:
                return "stale"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(mutate, ("player.pause", "player.seek"))
            )

        self.assertEqual(sorted(results), ["accepted", "stale"])
        persisted = getPlaybackContextState("context-1")
        self.assertEqual(persisted["version"], 2)
        self.assertEqual(persisted["controlVersion"], 2)

    def test_complete_and_cancel_handoff_have_one_atomic_terminal_winner(self):
        createStrictPlaybackContextState(
            "context-handoff",
            "alice",
            "source-1",
            "device:source-1",
            ["song-1"],
            0,
            100,
            "playing",
        )
        savePlaybackHandoff(
            {
                "handoffId": "handoff-1",
                "requestId": "handoff-start-1",
                "playbackContextId": "context-handoff",
                "userName": "alice",
                "sourceClientId": "source-1",
                "targetClientId": "target-1",
                "originClientId": "controller-1",
                "status": "committed",
                "baseControlVersion": 1,
                "controlVersion": 2,
                "prepareId": "prepare-1",
                "snapshot": {
                    "handoffControlVersion": 2,
                    "prepareId": "prepare-1",
                },
            }
        )
        barrier = threading.Barrier(2)

        def complete():
            barrier.wait()
            try:
                result = completeStrictPlaybackHandoff(
                    "context-handoff",
                    "handoff-1",
                    "alice",
                    "target-1",
                    "device:target-1",
                    position_ms=200,
                )
                return "completed" if result[3] else "completed_replay"
            except ValueError:
                return "complete_lost"

        def cancel():
            barrier.wait()
            result = terminateStrictPlaybackHandoff(
                "context-handoff",
                "handoff-1",
                "alice",
                "cancelled",
            )
            return "cancelled" if result[1] else "cancel_lost"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            complete_future = executor.submit(complete)
            cancel_future = executor.submit(cancel)
            results = {complete_future.result(), cancel_future.result()}

        handoff = getPlaybackHandoff("handoff-1")
        context = getPlaybackContextState("context-handoff")
        self.assertIn(
            results,
            (
                {"completed", "cancel_lost"},
                {"complete_lost", "cancelled"},
            ),
        )
        self.assertIn(handoff["status"], {"completed", "cancelled"})
        if handoff["status"] == "completed":
            self.assertEqual(context["authorityClientId"], "target-1")
            self.assertEqual(context["controlVersion"], 2)
        else:
            self.assertEqual(context["authorityClientId"], "source-1")
            self.assertEqual(context["controlVersion"], 1)

    def test_complete_and_timeout_handoff_have_one_atomic_terminal_winner(self):
        createStrictPlaybackContextState(
            "context-timeout",
            "alice",
            "source-1",
            "device:source-1",
            ["song-1"],
            0,
            100,
            "playing",
        )
        savePlaybackHandoff(
            {
                "handoffId": "handoff-timeout",
                "requestId": "handoff-start-timeout",
                "playbackContextId": "context-timeout",
                "userName": "alice",
                "sourceClientId": "source-1",
                "targetClientId": "target-1",
                "originClientId": "controller-1",
                "status": "committed",
                "baseControlVersion": 1,
                "controlVersion": 2,
                "prepareId": "prepare-timeout",
                "snapshot": {
                    "handoffControlVersion": 2,
                    "prepareId": "prepare-timeout",
                },
            }
        )
        barrier = threading.Barrier(2)

        def complete():
            barrier.wait()
            try:
                result = completeStrictPlaybackHandoff(
                    "context-timeout",
                    "handoff-timeout",
                    "alice",
                    "target-1",
                    "device:target-1",
                )
                return "completed" if result[3] else "completed_replay"
            except ValueError:
                return "complete_lost"

        def timeout():
            barrier.wait()
            result = terminateStrictPlaybackHandoff(
                "context-timeout",
                "handoff-timeout",
                "alice",
                "timed_out",
                error_code="commit_timeout",
            )
            return "timed_out" if result[1] else "timeout_lost"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            complete_future = executor.submit(complete)
            timeout_future = executor.submit(timeout)
            results = {complete_future.result(), timeout_future.result()}

        handoff = getPlaybackHandoff("handoff-timeout")
        context = getPlaybackContextState("context-timeout")
        self.assertIn(
            results,
            (
                {"completed", "timeout_lost"},
                {"complete_lost", "timed_out"},
            ),
        )
        if handoff["status"] == "completed":
            self.assertEqual(context["authorityClientId"], "target-1")
        else:
            self.assertEqual(handoff["status"], "timed_out")
            self.assertEqual(handoff["errorCode"], "commit_timeout")
            self.assertEqual(context["authorityClientId"], "source-1")

    def test_close_and_complete_handoff_are_linearized_by_context_tombstone(self):
        createStrictPlaybackContextState(
            "context-close-race",
            "alice",
            "source-1",
            "device:source-1",
            ["song-1"],
            0,
            100,
            "playing",
        )
        savePlaybackHandoff(
            {
                "handoffId": "handoff-close-race",
                "requestId": "handoff-start-close-race",
                "playbackContextId": "context-close-race",
                "userName": "alice",
                "sourceClientId": "source-1",
                "targetClientId": "target-1",
                "originClientId": "controller-1",
                "status": "committed",
                "baseControlVersion": 1,
                "controlVersion": 2,
                "prepareId": "prepare-close-race",
                "snapshot": {
                    "handoffControlVersion": 2,
                    "prepareId": "prepare-close-race",
                },
            }
        )
        barrier = threading.Barrier(2)

        def close_context():
            barrier.wait()
            closed = closeStrictPlaybackContextState(
                "context-close-race",
                "alice",
            )
            return "closed", closed

        def complete_handoff():
            barrier.wait()
            try:
                completed = completeStrictPlaybackHandoff(
                    "context-close-race",
                    "handoff-close-race",
                    "alice",
                    "target-1",
                    "device:target-1",
                )
                return "completed", completed[0]
            except PlaybackContextClosedError as exc:
                return "context_closed", exc.playback_context

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            close_future = executor.submit(close_context)
            complete_future = executor.submit(complete_handoff)
            outcomes = {
                close_future.result()[0],
                complete_future.result()[0],
            }

        context = getPlaybackContextState("context-close-race")
        handoff = getPlaybackHandoff("handoff-close-race")
        self.assertIn(
            outcomes,
            (
                {"closed", "context_closed"},
                {"closed", "completed"},
            ),
        )
        self.assertEqual(context["lifecycle"], "closed")
        if handoff["status"] == "completed":
            self.assertEqual(context["authorityClientId"], "target-1")
            self.assertEqual(context["version"], 3)
            self.assertEqual(context["controlVersion"], 2)
        else:
            self.assertEqual(handoff["status"], "failed")
            self.assertEqual(handoff["errorCode"], "context_closed")
            self.assertEqual(context["authorityClientId"], "source-1")
            self.assertEqual(context["version"], 2)
            self.assertEqual(context["controlVersion"], 1)

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

    def test_strict_create_persists_identity_fingerprint_and_initial_cursors(self):
        created_context, created = createStrictPlaybackContextState(
            "context-1",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-2", "song-1"],
            0,
            1200,
            "playing",
        )
        replayed_context, replayed = createStrictPlaybackContextState(
            "context-1",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-2", "song-1"],
            0,
            1200,
            "playing",
        )

        self.assertTrue(created)
        self.assertFalse(replayed)
        self.assertEqual(created_context, replayed_context)
        self.assertEqual(created_context["queueSongIds"], ["song-2", "song-1"])
        self.assertEqual(created_context["authorityDeviceSessionId"], "device:phone-1")
        self.assertEqual(created_context["lifecycle"], "active")
        self.assertRegex(created_context["creationFingerprint"], r"^[0-9a-f]{64}$")
        for cursor_name in ("epoch", "version", "queueRevision", "controlVersion"):
            self.assertEqual(created_context[cursor_name], 1)

    def test_strict_create_rejects_different_intent_and_closed_id(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-1"],
            0,
            0,
            "stopped",
        )

        with self.assertRaises(PlaybackContextIntentConflictError) as conflict:
            createStrictPlaybackContextState(
                "context-1",
                "alice",
                "phone-1",
                "device:phone-1",
                ["song-2"],
                0,
                0,
                "stopped",
            )
        self.assertEqual(conflict.exception.playback_context["version"], 1)

        closed = closeStrictPlaybackContextState("context-1", "alice")
        closed_again = closeStrictPlaybackContextState("context-1", "alice")
        self.assertEqual(closed["version"], 2)
        self.assertEqual(closed_again["version"], 2)
        self.assertEqual(closed_again["state"], "stopped")
        self.assertEqual(closed_again["lifecycle"], "closed")
        with self.assertRaises(PlaybackContextClosedError):
            createStrictPlaybackContextState(
                "context-1",
                "alice",
                "phone-1",
                "device:phone-1",
                ["song-1"],
                0,
                0,
                "stopped",
            )

    def test_restart_listing_preserves_active_and_closed_contexts(self):
        createStrictPlaybackContextState(
            "active-context",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-1"],
            0,
            0,
            "playing",
        )
        createStrictPlaybackContextState(
            "closed-context",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-2"],
            0,
            100,
            "paused",
        )
        closeStrictPlaybackContextState("closed-context", "alice")

        contexts = listPlaybackContexts()

        self.assertEqual(
            [context["playbackContextId"] for context in contexts],
            ["active-context", "closed-context"],
        )
        self.assertEqual(contexts[0]["authorityDeviceSessionId"], "device:phone-1")
        self.assertEqual(contexts[0]["version"], 1)
        self.assertEqual(contexts[1]["lifecycle"], "closed")
        self.assertEqual(contexts[1]["state"], "paused")
        self.assertEqual(contexts[1]["version"], 2)

    def test_restart_marks_nonterminal_handoff_failed(self):
        savePlaybackHandoff(
            {
                "handoffId": "handoff-active",
                "requestId": "request-active",
                "playbackContextId": "context-1",
                "userName": "alice",
                "sourceClientId": "phone-1",
                "targetClientId": "desktop-1",
                "status": "ready",
                "baseControlVersion": 1,
            }
        )

        reconciled = failActivePlaybackHandoffsForRestart()
        handoff = getPlaybackHandoff("handoff-active")

        self.assertEqual(reconciled, ["handoff-active"])
        self.assertEqual(handoff["status"], "failed")
        self.assertEqual(handoff["errorCode"], "server_restart")

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
        self.assertNotIn("logicalVolume", v2_context)
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
                "clientSeq": 1,
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
                "clientSeq": 1,
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
        self.assertEqual(v2_feedback["clientSeq"], 1)
        self.assertNotIn("outputDeviceId", v2_feedback)
        self.assertNotIn("audioDeviceName", v2_feedback)

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
