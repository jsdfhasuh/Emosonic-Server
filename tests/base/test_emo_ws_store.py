import concurrent.futures
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from supysonic import db
from supysonic.emo.ws_state import WebSocketState
from supysonic.emo.ws_store import (
    PlaybackContextAuthorityAmbiguousError,
    PlaybackContextBroadcastBarrierError,
    PlaybackContextClosedError,
    PlaybackContextEnsureConflictError,
    PlaybackContextIntentConflictError,
    PlaybackContextStaleVersionError,
    PlaybackControlTransactionConflictError,
    PlaybackControlReconciliationConflictError,
    PlaybackHandoffTargetConflictError,
    PlaybackLocalIntentConflictError,
    PlaybackPrepareAlreadyActiveError,
    PlaybackPrepareTransactionConflictError,
    PlaybackClientSequenceConflictError,
    closeStrictPlaybackContextState,
    applyStrictPlaybackUpdate,
    completeStrictPlaybackHandoff,
    createPlaybackContextState,
    createPlaybackControlTransaction,
    createPlaybackControlReconciliation,
    createPlaybackPrepareTransaction,
    createStrictPlaybackHandoff,
    createStrictPlaybackContextState,
    deletePlaybackContext,
    expirePlaybackContext,
    ensureStrictPlaybackContextState,
    failActivePlaybackHandoffsForRestart,
    getActivePlaybackHandoffs,
    getDevicePlaybackState,
    getDevicePlaybackStates,
    getPlaybackContextState,
    getPlaybackContextWithDeviceStates,
    getPlaybackControlTransaction,
    getPlaybackControlReconciliation,
    getPlaybackContextCloseTombstone,
    getPlaybackHandoff,
    getPlaybackHandoffByRequest,
    getPlaybackPrepareTransaction,
    getLocalQueueState,
    getPlaybackState,
    getPlaybackStates,
    getQueueState,
    listActivePlaybackContextBindings,
    listAllPendingPlaybackControlTransactions,
    listExpiredPlaybackControlTransactions,
    listPlaybackControlReconciliations,
    listExpiredPlaybackPrepareTransactions,
    listPendingPlaybackControlTransactions,
    listPendingPlaybackControlTransactionsForAuthorityConnection,
    listUserPlaybackContexts,
    listPlaybackContexts,
    mutateStrictPlaybackContextControl,
    mutateStrictPlaybackContextQueue,
    saveDevicePlaybackState,
    savePlaybackContextState,
    savePlaybackHandoff,
    savePlaybackLocalIntent,
    saveLocalQueueState,
    savePlaybackState,
    saveQueueState,
    serializeDevicePlaybackStateV2,
    serializePlaybackContextCloseTombstone,
    serializePlaybackControlTransaction,
    serializePlaybackControlReconciliation,
    serializePlaybackContextV2,
    settlePlaybackControlTransaction,
    settlePlaybackPrepareTransaction,
    terminateStrictPlaybackHandoff,
    updatePlaybackContextState,
    markPlaybackControlTransactionExecutionEligible,
)


class EmoWebSocketStoreTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp()
        os.close(handle)
        db.init_database("sqlite:///" + self.db_path)

    def tearDown(self):
        db.release_database()
        os.remove(self.db_path)

    def _create_exact_transaction(
        self,
        command_control_version=2,
        requesting_device_session_id="device:controller-1",
        requesting_connection_nonce="requester-nonce-1",
        requesting_connection_epoch=3,
        routed_connection_epoch=1,
        effective_at_server_ms=1500,
        accepted_at_ms=1000,
        execution_timeout_ms=15000,
        action="player.next",
    ):
        return createPlaybackControlTransaction(
            "context-1",
            "alice",
            1,
            command_control_version,
            "controller-1",
            "player-1",
            "device:player-1",
            "authority-nonce-1",
            routed_connection_epoch,
            action,
            {"queueIndex": 1, "trackId": "song-2"},
            accepted_at_ms,
            execution_timeout_ms,
            requesting_device_session_id=requesting_device_session_id,
            requesting_connection_nonce=requesting_connection_nonce,
            requesting_connection_epoch=requesting_connection_epoch,
            effective_at_server_ms=effective_at_server_ms,
        )

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

    def test_control_fails_closed_when_authority_pair_has_multiple_active_contexts(self):
        for context_id in ("context-1", "context-2"):
            createStrictPlaybackContextState(
                context_id,
                "alice",
                "player-1",
                "device:player-1",
                ["song-1"],
                0,
                0,
                "playing",
            )

        with self.assertRaises(
            PlaybackContextAuthorityAmbiguousError
        ) as conflict:
            mutateStrictPlaybackContextControl(
                "context-1",
                "alice",
                "controller-1",
                "player.pause",
                1,
            )

        canonical = conflict.exception.playback_context
        self.assertEqual(canonical["playbackContextId"], "context-1")
        self.assertEqual(canonical["controlVersion"], 1)
        self.assertEqual(canonical["queueRevision"], 1)
        self.assertEqual(canonical["version"], 1)
        self.assertEqual(getPlaybackContextState("context-1")["state"], "playing")

        closeStrictPlaybackContextState("context-2", "alice")
        updated = mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            1,
        )
        self.assertEqual(updated["state"], "paused")
        self.assertEqual(updated["controlVersion"], 2)

    def test_create_and_control_are_linearized_by_authority_pair(self):
        for iteration in range(10):
            first_context_id = "linear-context-%d-a" % iteration
            second_context_id = "linear-context-%d-b" % iteration
            client_id = "linear-player-%d" % iteration
            device_session_id = "device:linear-player-%d" % iteration
            createStrictPlaybackContextState(
                first_context_id,
                "alice",
                client_id,
                device_session_id,
                ["song-1"],
                0,
                0,
                "playing",
            )
            barrier = threading.Barrier(2)

            def create_second():
                barrier.wait()
                createStrictPlaybackContextState(
                    second_context_id,
                    "alice",
                    client_id,
                    device_session_id,
                    ["song-2"],
                    0,
                    0,
                    "playing",
                )
                return "created"

            def control_first():
                barrier.wait()
                try:
                    mutateStrictPlaybackContextControl(
                        first_context_id,
                        "alice",
                        "controller-1",
                        "player.pause",
                        1,
                    )
                    return "controlled"
                except PlaybackContextAuthorityAmbiguousError:
                    return "conflict"

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=2
            ) as executor:
                create_future = executor.submit(create_second)
                control_future = executor.submit(control_first)
                self.assertEqual(create_future.result(timeout=2), "created")
                control_result = control_future.result(timeout=2)

            canonical = getPlaybackContextState(first_context_id)
            if control_result == "controlled":
                self.assertEqual(canonical["state"], "paused")
                self.assertEqual(canonical["controlVersion"], 2)
            else:
                self.assertEqual(control_result, "conflict")
                self.assertEqual(canonical["state"], "playing")
                self.assertEqual(canonical["controlVersion"], 1)

    def test_close_and_control_are_linearized_by_context_and_authority_pair(self):
        for iteration in range(10):
            context_id = "close-control-context-%d" % iteration
            client_id = "close-control-player-%d" % iteration
            createStrictPlaybackContextState(
                context_id,
                "alice",
                client_id,
                "device:%s" % client_id,
                ["song-1"],
                0,
                0,
                "playing",
            )
            barrier = threading.Barrier(2)

            def close_context():
                barrier.wait()
                closeStrictPlaybackContextState(context_id, "alice")
                return "closed"

            def control_context():
                barrier.wait()
                try:
                    mutateStrictPlaybackContextControl(
                        context_id,
                        "alice",
                        "controller-1",
                        "player.pause",
                        1,
                    )
                    return "controlled"
                except PlaybackContextClosedError:
                    return "context_closed"

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=2
            ) as executor:
                close_future = executor.submit(close_context)
                control_future = executor.submit(control_context)
                self.assertEqual(close_future.result(timeout=2), "closed")
                control_result = control_future.result(timeout=2)

            canonical = getPlaybackContextState(context_id)
            self.assertEqual(canonical["lifecycle"], "closed")
            if control_result == "controlled":
                self.assertEqual(canonical["state"], "paused")
                self.assertEqual(canonical["controlVersion"], 2)
                self.assertEqual(canonical["version"], 3)
            else:
                self.assertEqual(control_result, "context_closed")
                self.assertEqual(canonical["state"], "playing")
                self.assertEqual(canonical["controlVersion"], 1)
                self.assertEqual(canonical["version"], 2)

    def test_handoff_and_control_are_linearized_by_authority_pair(self):
        for iteration in range(10):
            source_context_id = "handoff-control-source-%d" % iteration
            target_context_id = "handoff-control-target-%d" % iteration
            source_client_id = "handoff-control-source-player-%d" % iteration
            target_client_id = "handoff-control-target-player-%d" % iteration
            target_device_session_id = "device:%s" % target_client_id
            handoff_id = "handoff-control-%d" % iteration
            createStrictPlaybackContextState(
                source_context_id,
                "alice",
                source_client_id,
                "device:%s" % source_client_id,
                ["song-source"],
                0,
                0,
                "playing",
            )
            with mock.patch(
                "supysonic.emo.ws_store._new_playback_context_id",
                return_value=target_context_id,
            ):
                ensureStrictPlaybackContextState(
                    "alice",
                    target_client_id,
                    target_device_session_id,
                    [],
                    None,
                    0,
                    "idle",
                )
            handoff, created = createStrictPlaybackHandoff(
                source_context_id,
                {
                    "handoffId": handoff_id,
                    "requestId": "handoff-control-start-%d" % iteration,
                    "playbackContextId": source_context_id,
                    "userName": "alice",
                    "sourceClientId": source_client_id,
                    "targetClientId": target_client_id,
                    "originClientId": "controller-1",
                    "status": "preparing",
                    "baseControlVersion": 1,
                    "controlVersion": 2,
                    "prepareId": "prepare-handoff-control-%d" % iteration,
                    "snapshot": {
                        "handoffControlVersion": 2,
                        "prepareId": "prepare-handoff-control-%d" % iteration,
                    },
                },
                target_device_session_id,
            )
            self.assertTrue(created)
            handoff["status"] = "committed"
            savePlaybackHandoff(handoff)
            barrier = threading.Barrier(2)

            def complete_handoff():
                barrier.wait()
                try:
                    return completeStrictPlaybackHandoff(
                        source_context_id,
                        handoff_id,
                        "alice",
                        target_client_id,
                        target_device_session_id,
                    )
                except PlaybackHandoffTargetConflictError:
                    return "conflict"

            def control_target_context():
                barrier.wait()
                try:
                    mutateStrictPlaybackContextQueue(
                        target_context_id,
                        "alice",
                        target_client_id,
                        target_device_session_id,
                        ["song-target"],
                        0,
                        0,
                        1,
                        base_control_version=1,
                    )
                    return "initialized"
                except PlaybackContextClosedError:
                    return "context_closed"

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=2
            ) as executor:
                handoff_future = executor.submit(complete_handoff)
                control_future = executor.submit(control_target_context)
                handoff_result = handoff_future.result(timeout=2)
                control_result = control_future.result(timeout=2)

            source_context = getPlaybackContextState(source_context_id)
            target_context = getPlaybackContextState(target_context_id)
            if handoff_result == "conflict":
                self.assertEqual(control_result, "initialized")
                self.assertEqual(
                    source_context["authorityClientId"],
                    source_client_id,
                )
                self.assertEqual(target_context["lifecycle"], "active")
                self.assertEqual(target_context["state"], "paused")
                self.assertEqual(target_context["controlVersion"], 2)
                self.assertEqual(
                    getPlaybackHandoff(handoff_id)["status"],
                    "committed",
                )
                continue

            self.assertTrue(handoff_result.mutated)
            self.assertEqual(control_result, "context_closed")
            self.assertEqual(
                handoff_result.affected_authority_pairs,
                tuple(
                    sorted(
                        (
                            (
                                "alice",
                                source_client_id,
                                "device:%s" % source_client_id,
                            ),
                            (
                                "alice",
                                target_client_id,
                                target_device_session_id,
                            ),
                        )
                    )
                ),
            )
            self.assertEqual(
                source_context["authorityClientId"],
                target_client_id,
            )
            self.assertEqual(
                handoff_result.canonical_context,
                source_context,
            )
            self.assertEqual(getPlaybackHandoff(handoff_id)["status"], "completed")
            self.assertEqual(target_context["lifecycle"], "closed")
            self.assertEqual(
                handoff_result.retired_context["playbackContextId"],
                target_context_id,
            )

            replay_result = completeStrictPlaybackHandoff(
                source_context_id,
                handoff_id,
                "alice",
                target_client_id,
                target_device_session_id,
            )
            self.assertFalse(replay_result.mutated)
            self.assertEqual(replay_result.affected_authority_pairs, ())
            self.assertEqual(
                replay_result.canonical_context,
                getPlaybackContextState(source_context_id),
            )

    def test_handoff_start_rejects_non_idle_or_preparing_target_context(self):
        createStrictPlaybackContextState(
            "handoff-start-source",
            "alice",
            "source-player",
            "device:source-player",
            ["song-source"],
            0,
            0,
            "playing",
        )
        createStrictPlaybackContextState(
            "handoff-start-target",
            "alice",
            "target-player",
            "device:target-player",
            ["song-target"],
            0,
            0,
            "paused",
        )
        handoff = {
            "handoffId": "handoff-start-conflict",
            "requestId": "handoff-start-conflict-request",
            "playbackContextId": "handoff-start-source",
            "userName": "alice",
            "sourceClientId": "source-player",
            "targetClientId": "target-player",
            "originClientId": "controller-1",
            "status": "preparing",
            "baseControlVersion": 1,
            "controlVersion": 2,
            "prepareId": "handoff-start-conflict-prepare",
            "snapshot": {
                "handoffControlVersion": 2,
                "prepareId": "handoff-start-conflict-prepare",
            },
        }

        with self.assertRaises(PlaybackHandoffTargetConflictError):
            createStrictPlaybackHandoff(
                "handoff-start-source",
                handoff,
                "device:target-player",
            )
        self.assertIsNone(getPlaybackHandoff("handoff-start-conflict"))

        closeStrictPlaybackContextState("handoff-start-target", "alice")
        with mock.patch(
            "supysonic.emo.ws_store._new_playback_context_id",
            return_value="handoff-start-target-idle",
        ):
            ensureStrictPlaybackContextState(
                "alice",
                "target-player",
                "device:target-player",
                [],
                None,
                0,
                "idle",
            )
        createPlaybackPrepareTransaction(
            "handoff-start-target-idle",
            "alice",
            1,
            "target-prepare-intent",
            "controller-1",
            "target-player",
            "device:target-player",
            "target-nonce",
            1,
            {"baseControlVersion": 1},
            1,
            10000,
        )

        with self.assertRaises(PlaybackHandoffTargetConflictError):
            createStrictPlaybackHandoff(
                "handoff-start-source",
                handoff,
                "device:target-player",
            )
        self.assertIsNone(getPlaybackHandoff("handoff-start-conflict"))

    def test_strict_context_creates_serialize_sqlite_write_upgrade(self) -> None:
        second_read = threading.Event()
        read_lock = threading.Lock()
        read_count = 0
        original_get = db.EmoPlaybackContext.get_or_none

        def synchronized_get(*args: object, **kwargs: object) -> object:
            nonlocal read_count
            record = original_get(*args, **kwargs)
            with read_lock:
                read_count += 1
                current_read = read_count
            if current_read == 1:
                second_read.wait(timeout=0.25)
            else:
                second_read.set()
            return record

        def create(index: int) -> bool:
            _context, created = createStrictPlaybackContextState(
                "context-%d" % index,
                "alice",
                "player-%d" % index,
                "device:player-%d" % index,
                ["song-%d" % index],
                0,
                0,
                "playing",
            )
            return created

        with mock.patch.object(
            db.EmoPlaybackContext,
            "get_or_none",
            side_effect=synchronized_get,
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(create, (1, 2)))

        self.assertEqual(results, [True, True])
        self.assertEqual(
            [context["playbackContextId"] for context in listPlaybackContexts()],
            ["context-1", "context-2"],
        )

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
        created_result = createStrictPlaybackContextState(
            "context-1",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-2", "song-1"],
            0,
            1200,
            "playing",
        )
        replayed_result = createStrictPlaybackContextState(
            "context-1",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-2", "song-1"],
            0,
            1200,
            "playing",
        )
        created_context, created = created_result
        replayed_context, replayed = replayed_result

        self.assertTrue(created)
        self.assertFalse(replayed)
        self.assertTrue(created_result.mutated)
        self.assertEqual(
            created_result.affected_authority_pairs,
            (("alice", "phone-1", "device:phone-1"),),
        )
        self.assertEqual(created_result.canonical_context, created_context)
        self.assertFalse(replayed_result.mutated)
        self.assertEqual(replayed_result.affected_authority_pairs, ())
        self.assertEqual(replayed_result.canonical_context, replayed_context)
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
        self.assertTrue(closed.mutated)
        self.assertEqual(
            closed.affected_authority_pairs,
            (("alice", "phone-1", "device:phone-1"),),
        )
        self.assertEqual(closed.canonical_context, dict(closed))
        self.assertFalse(closed_again.mutated)
        self.assertEqual(closed_again.affected_authority_pairs, ())
        self.assertEqual(closed_again.canonical_context, dict(closed_again))
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

    def test_list_active_playback_context_bindings_filters_exact_pair_and_user(self):
        for context_id, user_name, client_id, device_session_id in (
            ("context-b", "alice", "player-1", "device:player-1"),
            ("context-a", "alice", "player-1", "device:player-1"),
            ("context-c", "alice", "player-1", "device:player-1"),
            ("context-other-device", "alice", "player-1", "device:player-2"),
            ("context-other-user", "bob", "player-1", "device:player-1"),
        ):
            createStrictPlaybackContextState(
                context_id,
                user_name,
                client_id,
                device_session_id,
                ["song-1"],
                0,
                0,
                "playing",
            )
        closeStrictPlaybackContextState("context-b", "alice")

        bindings = listActivePlaybackContextBindings(
            "alice",
            "player-1",
            "device:player-1",
        )

        self.assertEqual(
            bindings,
            [
                {
                    "playbackContextId": "context-a",
                    "authorityClientId": "player-1",
                    "authorityDeviceSessionId": "device:player-1",
                },
                {
                    "playbackContextId": "context-c",
                    "authorityClientId": "player-1",
                    "authorityDeviceSessionId": "device:player-1",
                }
            ],
        )
        self.assertEqual(
            listActivePlaybackContextBindings(
                "alice",
                "player-1",
                "device:missing",
            ),
            [],
        )
        self.assertEqual(
            listActivePlaybackContextBindings(
                "carol",
                "player-1",
                "device:player-1",
            ),
            [],
        )

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
                "epoch": 1,
                "appliedControlVersion": 1,
                "clientSeq": 1,
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
                "appliedControlVersion": 1,
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
                "appliedControlVersion": 1,
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
        self.assertEqual(v2_feedback["appliedControlVersion"], 1)
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

    def test_control_transaction_is_persistent_ordered_and_terminal_idempotent(self):
        transaction, created = createPlaybackControlTransaction(
            "context-1",
            "alice",
            1,
            2,
            "controller-1",
            "player-1",
            "device:player-1",
            "nonce-1",
            1,
            "player.next",
            {"queueIndex": 1, "trackId": "song-2"},
            1000,
            15000,
        )

        self.assertTrue(created)
        self.assertEqual(transaction["status"], "pending")
        self.assertEqual(transaction["watchdogDeadlineAtMs"], 18000)
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2),
            transaction,
        )
        self.assertEqual(listExpiredPlaybackControlTransactions(17999), [])
        self.assertEqual(
            [item["commandControlVersion"] for item in listPendingPlaybackControlTransactions("context-1", 1)],
            [2],
        )
        self.assertEqual(
            [
                item["commandControlVersion"]
                for item in listPendingPlaybackControlTransactionsForAuthorityConnection(
                    "alice",
                    "player-1",
                    "device:player-1",
                    "nonce-1",
                )
            ],
            [2],
        )
        self.assertEqual(
            [
                item["commandControlVersion"]
                for item in listAllPendingPlaybackControlTransactions()
            ],
            [2],
        )
        self.assertEqual(
            [item["commandControlVersion"] for item in listExpiredPlaybackControlTransactions(18000)],
            [2],
        )

        terminal, changed = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "failed",
            18000,
            error_code="execution_unknown",
            applied_control_version=1,
        )
        self.assertTrue(changed)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(listPendingPlaybackControlTransactions("context-1", 1), [])

        replay, changed = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "failed",
            18000,
            error_code="execution_unknown",
            applied_control_version=1,
        )
        self.assertFalse(changed)
        self.assertEqual(replay, terminal)

        with self.assertRaises(PlaybackControlTransactionConflictError):
            settlePlaybackControlTransaction(
                "context-1",
                1,
                2,
                "committed",
                18001,
                applied_control_version=2,
            )

    def test_exact_control_transaction_round_trips_requester_and_new_fields(self):
        transaction, created = self._create_exact_transaction(
            effective_at_server_ms=1500,
        )

        self.assertTrue(created)
        self.assertEqual(transaction["requestingDeviceSessionId"], "device:controller-1")
        self.assertEqual(transaction["requestingConnectionNonce"], "requester-nonce-1")
        self.assertEqual(transaction["requestingConnectionEpoch"], 3)
        self.assertEqual(transaction["effectiveAtServerMs"], 1500)
        self.assertNotIn("executionEligibleAtMs", transaction)
        self.assertNotIn("watchdogDeadlineAtMs", transaction)
        self.assertEqual(
            transaction["acceptedTarget"],
            {"queueIndex": 1, "trackId": "song-2"},
        )
        self.assertNotIn("id", transaction)
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2),
            transaction,
        )

        db.release_database()
        db.init_database("sqlite:///" + self.db_path)
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2),
            transaction,
        )

    def test_control_transaction_requester_generation_is_all_or_none(self):
        invalid_inputs = (
            {"requesting_device_session_id": None},
            {"requesting_connection_nonce": None},
            {"requesting_connection_epoch": None},
            {
                "requesting_device_session_id": "",
                "requesting_connection_nonce": "nonce",
                "requesting_connection_epoch": 1,
            },
            {
                "requesting_device_session_id": "device",
                "requesting_connection_nonce": "nonce",
                "requesting_connection_epoch": True,
            },
            {
                "requesting_device_session_id": "device",
                "requesting_connection_nonce": "nonce",
                "requesting_connection_epoch": 0,
            },
        )
        for index, overrides in enumerate(invalid_inputs, start=2):
            arguments = {
                "requesting_device_session_id": "device:controller-1",
                "requesting_connection_nonce": "requester-nonce-1",
                "requesting_connection_epoch": 3,
            }
            arguments.update(overrides)
            with self.assertRaises(ValueError):
                self._create_exact_transaction(
                    command_control_version=index,
                    **arguments,
                )
            self.assertEqual(
                db.EmoPlaybackControlTransaction.select().count(),
                0,
            )

    def test_exact_control_transaction_creation_is_idempotent_by_full_identity(self):
        first, created = self._create_exact_transaction()
        replay, replay_created = self._create_exact_transaction()
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay, first)

        conflicting_arguments = (
            {"requesting_device_session_id": "device:replacement"},
            {"requesting_connection_nonce": "requester-nonce-2"},
            {"requesting_connection_epoch": 4},
            {"routed_connection_epoch": 2},
            {"effective_at_server_ms": 1600},
        )
        for overrides in conflicting_arguments:
            with self.assertRaises(PlaybackControlTransactionConflictError):
                self._create_exact_transaction(**overrides)

    def test_execution_eligibility_is_atomic_idempotent_and_starts_watchdog(self):
        self._create_exact_transaction(effective_at_server_ms=1500, execution_timeout_ms=100)
        self.assertEqual(listExpiredPlaybackControlTransactions(100000), [])

        eligible, changed = markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1600,
        )
        self.assertTrue(changed)
        self.assertEqual(eligible["executionEligibleAtMs"], 1600)
        self.assertEqual(eligible["watchdogDeadlineAtMs"], 3700)
        self.assertEqual(listExpiredPlaybackControlTransactions(3699), [])
        self.assertEqual(
            [item["commandControlVersion"]
             for item in listExpiredPlaybackControlTransactions(3700)],
            [2],
        )

        persisted = db.EmoPlaybackControlTransaction.get(
            (db.EmoPlaybackControlTransaction.playback_context_id == "context-1")
            & (db.EmoPlaybackControlTransaction.epoch == 1)
            & (db.EmoPlaybackControlTransaction.command_control_version == 2)
        )
        updated_at = persisted.updated_at
        replay, replay_changed = markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1600,
        )
        self.assertFalse(replay_changed)
        self.assertEqual(replay, eligible)
        self.assertEqual(
            db.EmoPlaybackControlTransaction.get_by_id(persisted.id).updated_at,
            updated_at,
        )
        with self.assertRaises(PlaybackControlTransactionConflictError):
            markPlaybackControlTransactionExecutionEligible(
                "context-1",
                1,
                2,
                1601,
            )

        missing = markPlaybackControlTransactionExecutionEligible(
            "missing-context",
            1,
            1,
            100,
        )
        self.assertEqual(missing, (None, False))
        with self.assertRaises(ValueError):
            markPlaybackControlTransactionExecutionEligible(
                "context-1",
                1,
                2,
                True,
            )

        self._create_exact_transaction(
            command_control_version=3,
            effective_at_server_ms=2000,
        )
        with self.assertRaises(ValueError):
            markPlaybackControlTransactionExecutionEligible(
                "context-1",
                1,
                3,
                1999,
            )
        self.assertNotIn(
            "executionEligibleAtMs",
            getPlaybackControlTransaction("context-1", 1, 3),
        )

        self._create_exact_transaction(command_control_version=4)
        settlePlaybackControlTransaction(
            "context-1",
            1,
            4,
            "failed",
            2000,
            error_code="execution_unknown",
        )
        with self.assertRaises(PlaybackControlTransactionConflictError):
            markPlaybackControlTransactionExecutionEligible(
                "context-1",
                1,
                4,
                2000,
            )

    def test_execution_eligibility_allows_active_broadcast_source_fence(self):
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
        self._create_exact_transaction(effective_at_server_ms=1500, execution_timeout_ms=100)
        db.EmoBroadcastFence.create(
            resource_key="broadcast-source-fence-1",
            broadcast_id="broadcast-1",
            user_name="alice",
            role="source",
            phase="nonterminal",
            playback_context_id="context-1",
            client_id="player-1",
            device_session_id="device:player-1",
        )

        eligible, changed = markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1600,
        )
        self.assertTrue(changed)
        self.assertEqual(eligible["executionEligibleAtMs"], 1600)
        self.assertEqual(eligible["watchdogDeadlineAtMs"], 3700)

        replay, replay_changed = markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1600,
        )
        self.assertFalse(replay_changed)
        self.assertEqual(replay, eligible)

    def test_execution_eligibility_remains_blocked_by_ordinary_broadcast_fence(self):
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
        self._create_exact_transaction(effective_at_server_ms=1500, execution_timeout_ms=100)
        db.EmoBroadcastFence.create(
            resource_key="broadcast-ordinary-fence-1",
            broadcast_id="broadcast-1",
            user_name="alice",
            role="ordinary",
            phase="nonterminal",
            playback_context_id="context-1",
            client_id="player-1",
            device_session_id="device:player-1",
        )

        with self.assertRaises(PlaybackContextBroadcastBarrierError):
            markPlaybackControlTransactionExecutionEligible(
                "context-1",
                1,
                2,
                1600,
            )

        transaction = getPlaybackControlTransaction("context-1", 1, 2)
        self.assertNotIn("executionEligibleAtMs", transaction)
        self.assertNotIn("watchdogDeadlineAtMs", transaction)

    def test_expired_control_query_excludes_nullable_and_terminal_deadlines(self):
        self._create_exact_transaction(command_control_version=2)
        createPlaybackControlTransaction(
            "context-1",
            "alice",
            1,
            3,
            "controller-1",
            "player-1",
            "device:player-1",
            "authority-nonce-1",
            1,
            "player.pause",
            {"state": "paused"},
            1000,
            15000,
        )
        createPlaybackControlTransaction(
            "context-1",
            "alice",
            1,
            4,
            "controller-1",
            "player-1",
            "device:player-1",
            "authority-nonce-1",
            1,
            "player.play",
            {"state": "playing"},
            1000,
            15000,
        )
        settlePlaybackControlTransaction(
            "context-1",
            1,
            4,
            "failed",
            2000,
            error_code="execution_unknown",
        )
        self.assertEqual(
            [item["commandControlVersion"]
             for item in listExpiredPlaybackControlTransactions(18000)],
            [3],
        )
        with self.assertRaises(ValueError):
            listExpiredPlaybackControlTransactions(False)

    def test_authority_pending_query_matches_routed_epoch_when_supplied(self):
        self._create_exact_transaction(command_control_version=2, routed_connection_epoch=1)
        self._create_exact_transaction(command_control_version=3, routed_connection_epoch=2)

        self.assertEqual(
            [item["commandControlVersion"]
             for item in listPendingPlaybackControlTransactionsForAuthorityConnection(
                 "alice",
                 "player-1",
                 "device:player-1",
                 "authority-nonce-1",
             )],
            [2, 3],
        )
        self.assertEqual(
            [item["commandControlVersion"]
             for item in listPendingPlaybackControlTransactionsForAuthorityConnection(
                 "alice",
                 "player-1",
                 "device:player-1",
                 "authority-nonce-1",
                 1,
             )],
            [2],
        )
        self.assertEqual(
            [item["commandControlVersion"]
             for item in listPendingPlaybackControlTransactionsForAuthorityConnection(
                 "alice",
                 "player-1",
                 "device:player-1",
                 "authority-nonce-1",
                 2,
             )],
            [3],
        )

    def test_terminal_error_message_is_persistent_and_part_of_idempotency(self):
        self._create_exact_transaction(command_control_version=2)
        terminal, changed = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "failed",
            2000,
            error_code="dependency_failed",
            error_message="dependency command failed",
        )
        self.assertTrue(changed)
        self.assertEqual(terminal["errorMessage"], "dependency command failed")
        self.assertIsNotNone(terminal["terminalFingerprint"])
        replay, replay_changed = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "failed",
            2000,
            error_code="dependency_failed",
            error_message="dependency command failed",
        )
        self.assertFalse(replay_changed)
        self.assertEqual(replay, terminal)
        with self.assertRaises(PlaybackControlTransactionConflictError):
            settlePlaybackControlTransaction(
                "context-1",
                1,
                2,
                "failed",
                2000,
                error_code="dependency_failed",
                error_message="different diagnostic",
            )

        self._create_exact_transaction(command_control_version=3)
        with self.assertRaises(ValueError):
            settlePlaybackControlTransaction(
                "context-1",
                1,
                3,
                "committed",
                2000,
                error_message="not allowed",
            )
        db.release_database()
        db.init_database("sqlite:///" + self.db_path)
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2),
            terminal,
        )

    def test_legacy_terminal_fingerprint_replays_without_error_message(self):
        self._create_exact_transaction(command_control_version=2)
        record = db.EmoPlaybackControlTransaction.get(
            (db.EmoPlaybackControlTransaction.playback_context_id == "context-1")
            & (db.EmoPlaybackControlTransaction.epoch == 1)
            & (db.EmoPlaybackControlTransaction.command_control_version == 2)
        )
        old_terminal = {
            "status": "failed",
            "errorCode": "execution_unknown",
            "dependsOnControlVersion": None,
            "appliedControlVersion": 1,
        }
        old_fingerprint = hashlib.sha256(
            json.dumps(
                old_terminal,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        record.status = "failed"
        record.error_code = "execution_unknown"
        record.depends_on_control_version = None
        record.applied_control_version = 1
        record.error_message = None
        record.terminal_fingerprint = old_fingerprint
        record.terminal_at_ms = 2000
        record.save()
        before = db.EmoPlaybackControlTransaction.get_by_id(record.id)

        replay, changed = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "failed",
            2000,
            error_code="execution_unknown",
            applied_control_version=1,
        )
        self.assertFalse(changed)
        self.assertEqual(replay["status"], "failed")
        self.assertEqual(replay["errorCode"], "execution_unknown")
        self.assertEqual(replay["appliedControlVersion"], 1)
        self.assertEqual(replay["terminalFingerprint"], old_fingerprint)
        self.assertNotIn("errorMessage", replay)

        after = db.EmoPlaybackControlTransaction.get_by_id(record.id)
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertEqual(after.terminal_fingerprint, old_fingerprint)
        self.assertIsNone(after.error_message)

        with self.assertRaises(PlaybackControlTransactionConflictError):
            settlePlaybackControlTransaction(
                "context-1",
                1,
                2,
                "failed",
                2000,
                error_code="execution_unknown",
                applied_control_version=1,
                error_message="late diagnostic",
            )

    def test_transaction_serializer_fails_closed_for_malformed_target_json(self):
        record = db.EmoPlaybackControlTransaction.create(
            playback_context_id="context-1",
            user_name="alice",
            epoch=1,
            command_control_version=2,
            requesting_client_id="controller-1",
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="authority-nonce-1",
            routed_connection_epoch=1,
            action="player.pause",
            accepted_target_json="not-json",
            status="pending",
            accepted_at_ms=1000,
            execution_timeout_ms=15000,
        )
        with self.assertRaises(ValueError):
            serializePlaybackControlTransaction(record)

    def test_reconciliation_store_is_canonical_idempotent_and_persistent(self):
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
        before_context = getPlaybackContextState("context-1")
        actual_fact = {"positionMs": 120, "state": "playing"}
        canonical_update = {"appliedControlVersion": 1, "positionMs": 120}
        first, created = createPlaybackControlReconciliation(
            "context-1",
            "alice",
            1,
            2,
            0,
            1,
            "terminal_gap",
            actual_fact,
            canonical_update,
            2000,
            trigger_command_control_version=1,
        )
        self.assertTrue(created)
        expected_fingerprint = hashlib.sha256(
            json.dumps(
                actual_fact,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(first["actualFactFingerprint"], expected_fingerprint)
        self.assertEqual(
            serializePlaybackControlReconciliation(
                db.EmoPlaybackControlReconciliation.get_by_id(
                    db.EmoPlaybackControlReconciliation.select()
                    .where(
                        db.EmoPlaybackControlReconciliation.playback_context_id
                        == "context-1"
                    )
                    .get()
                    .id
                )
            ),
            first,
        )
        replay, replay_created = createPlaybackControlReconciliation(
            "context-1",
            "alice",
            1,
            2,
            0,
            1,
            "terminal_gap",
            actual_fact,
            canonical_update,
            2000,
            trigger_command_control_version=1,
        )
        self.assertFalse(replay_created)
        self.assertEqual(replay, first)
        for overrides in (
            {"actual_fact": {"positionMs": 121, "state": "playing"}},
            {"canonical_update": {"appliedControlVersion": 2}},
            {"trigger_kind": "other"},
            {"server_updated_at_ms": 2001},
        ):
            arguments = {
                "playback_context_id": "context-1",
                "user_name": "alice",
                "epoch": 1,
                "reconciliation_control_version": 2,
                "from_applied_control_version": 0,
                "through_control_version": 1,
                "trigger_kind": "terminal_gap",
                "actual_fact": actual_fact,
                "canonical_update": canonical_update,
                "server_updated_at_ms": 2000,
                "trigger_command_control_version": 1,
            }
            arguments.update(overrides)
            with self.assertRaises(PlaybackControlReconciliationConflictError):
                createPlaybackControlReconciliation(**arguments)

        second, second_created = createPlaybackControlReconciliation(
            "context-1",
            "alice",
            1,
            3,
            1,
            2,
            "terminal_gap",
            {"positionMs": 130},
            {"appliedControlVersion": 2},
            2100,
        )
        self.assertTrue(second_created)
        self.assertEqual(
            [item["reconciliationControlVersion"]
             for item in listPlaybackControlReconciliations("context-1", 1)],
            [2, 3],
        )
        self.assertEqual(
            getPlaybackControlReconciliation("context-1", 1, 2),
            first,
        )
        self.assertEqual(getPlaybackContextState("context-1"), before_context)
        db.release_database()
        db.init_database("sqlite:///" + self.db_path)
        self.assertEqual(
            listPlaybackControlReconciliations("context-1", 1),
            [first, second],
        )

    def test_close_tombstone_read_is_user_scoped_and_does_not_infer_legacy_fields(self):
        createStrictPlaybackContextState(
            "closed-context",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            0,
            "playing",
        )
        closed = db.EmoPlaybackContext.get(
            db.EmoPlaybackContext.playback_context_id == "closed-context"
        )
        closed.lifecycle = "closed"
        closed.close_action = "playback.context.close"
        closed.close_request_fingerprint = "f" * 64
        closed.close_expected_epoch = 1
        closed.close_base_version = 4
        closed.closed_from_epoch = 1
        closed.closed_from_version = 4
        closed.final_epoch = 1
        closed.final_version = 5
        closed.final_queue_revision = 2
        closed.final_control_version = 4
        closed.close_outcome_json = json.dumps(
            {"action": "playback.context.close", "status": "closed"}
        )
        closed.save()

        tombstone = getPlaybackContextCloseTombstone("closed-context", "alice")
        self.assertEqual(tombstone["playbackContextId"], "closed-context")
        self.assertEqual(tombstone["closeAction"], "playback.context.close")
        self.assertEqual(tombstone["closedFromVersion"], 4)
        self.assertEqual(tombstone["finalControlVersion"], 4)
        self.assertEqual(
            serializePlaybackContextCloseTombstone(closed),
            tombstone,
        )
        self.assertIsNone(
            getPlaybackContextCloseTombstone("closed-context", "bob")
        )
        self.assertIsNone(
            getPlaybackContextCloseTombstone("active-context", "alice")
        )

        createStrictPlaybackContextState(
            "legacy-closed",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            0,
            "playing",
        )
        legacy = db.EmoPlaybackContext.get(
            db.EmoPlaybackContext.playback_context_id == "legacy-closed"
        )
        legacy.lifecycle = "closed"
        legacy.save()
        self.assertEqual(
            getPlaybackContextCloseTombstone("legacy-closed", "alice"),
            {
                "playbackContextId": "legacy-closed",
                "userName": "alice",
                "lifecycle": "closed",
            },
        )
        closed.close_outcome_json = "[]"
        closed.save()
        with self.assertRaises(ValueError):
            getPlaybackContextCloseTombstone("closed-context", "alice")

    def test_control_mutation_rolls_back_context_when_transaction_creation_fails(self):
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
        before = getPlaybackContextState("context-1")
        with mock.patch(
            "supysonic.emo.ws_store._create_playback_control_transaction_record",
            side_effect=RuntimeError("injected transaction failure"),
        ):
            with self.assertRaises(RuntimeError):
                mutateStrictPlaybackContextControl(
                    "context-1",
                    "alice",
                    "controller-1",
                    "player.pause",
                    1,
                    requesting_client_id="controller-1",
                    authority_client_id="player-1",
                    authority_device_session_id="device:player-1",
                    routed_connection_nonce="authority-nonce-1",
                    routed_connection_epoch=1,
                    accepted_at_ms=1000,
                    execution_timeout_ms=15000,
                    requesting_device_session_id="device:controller-1",
                    requesting_connection_nonce="requester-nonce-1",
                    requesting_connection_epoch=3,
                )
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertEqual(
            db.EmoPlaybackControlTransaction.select().count(),
            0,
        )

    def test_strict_playback_update_commits_pending_and_advances_applied_cursor(self):
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
        passive = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 10,
                "clientSeq": 1,
            },
            1000,
        )
        self.assertEqual(passive["canonicalUpdate"]["appliedControlVersion"], 1)

        mutated = mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            1,
            requesting_client_id="controller-1",
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="nonce-1",
            accepted_at_ms=1100,
            execution_timeout_ms=15000,
        )
        self.assertEqual(mutated["controlVersion"], 2)

        committed = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "remoteCommand",
                "executionStatus": "committed",
                "commandControlVersion": 2,
                "appliedControlVersion": 2,
                "state": "paused",
                "trackId": "song-1",
                "positionMs": 10,
                "clientSeq": 2,
            },
            1200,
        )
        transaction = getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(transaction["status"], "committed")
        self.assertEqual(committed["canonicalUpdate"]["controlVersion"], 2)
        self.assertEqual(committed["canonicalUpdate"]["appliedControlVersion"], 2)

        duplicate = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "remoteCommand",
                "executionStatus": "committed",
                "commandControlVersion": 2,
                "appliedControlVersion": 2,
                "state": "paused",
                "trackId": "song-1",
                "positionMs": 10,
                "clientSeq": 2,
            },
            1300,
        )
        self.assertTrue(duplicate["sourceOnly"])
        self.assertEqual(duplicate["canonicalUpdate"], committed["canonicalUpdate"])

    def test_failed_track_change_cascades_dependency_terminals_in_version_order(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2"],
            0,
            0,
            "playing",
        )
        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 0,
                "clientSeq": 1,
            },
            1000,
        )
        first = mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.next",
            1,
            requesting_client_id="controller-1",
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="nonce-1",
            accepted_at_ms=1100,
            execution_timeout_ms=15000,
        )
        mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            first["controlVersion"],
            requesting_client_id="controller-1",
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="nonce-1",
            accepted_at_ms=1200,
            execution_timeout_ms=15000,
        )

        failed = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "remoteCommand",
                "executionStatus": "failed",
                "commandControlVersion": 2,
                "appliedControlVersion": 1,
                "errorCode": "track_load_failed",
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 0,
                "clientSeq": 2,
            },
            1300,
        )

        self.assertEqual(
            [item["commandControlVersion"] for item in failed["dependencySettlements"]],
            [3],
        )
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 3)["errorCode"],
            "dependency_failed",
        )
        context = getPlaybackContextState("context-1")
        self.assertEqual(context["controlVersion"], 3)
        self.assertEqual(context["currentIndex"], 0)
        self.assertEqual(context["trackId"], "song-1")

    def test_local_user_update_allocates_version_and_supersedes_pending(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2"],
            0,
            0,
            "playing",
        )
        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 0,
                "clientSeq": 1,
            },
            1000,
        )
        mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            1,
            requesting_client_id="controller-1",
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="nonce-1",
            accepted_at_ms=1100,
            execution_timeout_ms=15000,
        )

        local = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "localUser",
                "executionStatus": "committed",
                "intentId": "local-1",
                "epoch": 1,
                "observedControlVersion": 2,
                "queueIndex": 1,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 0,
                "clientSeq": 2,
            },
            1200,
        )

        self.assertEqual(local["canonicalUpdate"]["controlVersion"], 3)
        self.assertEqual(
            local["canonicalUpdate"]["supersededThroughControlVersion"],
            2,
        )
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2)["status"],
            "superseded",
        )
        self.assertEqual(getPlaybackContextState("context-1")["currentIndex"], 1)

    def test_local_user_seek_advances_control_after_passive_progress(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2"],
            0,
            100,
            "playing",
        )
        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 100,
                "clientSeq": 1,
            },
            1000,
        )
        before_progress = getPlaybackContextState("context-1")

        progress = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 250,
                "clientSeq": 2,
            },
            1100,
        )
        self.assertTrue(progress["created"])
        self.assertEqual(
            getPlaybackContextState("context-1"),
            before_progress,
        )
        progress_device = getDevicePlaybackState("context-1", "player-1")
        self.assertEqual(progress_device["positionMs"], 250)
        self.assertEqual(progress_device["appliedControlVersion"], 1)

        local_seek = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "localUser",
                "executionStatus": "committed",
                "intentId": "local-seek-1",
                "epoch": 1,
                "observedControlVersion": 1,
                "queueIndex": 0,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 500,
                "clientSeq": 3,
            },
            1200,
        )
        context = getPlaybackContextState("context-1")
        device = getDevicePlaybackState("context-1", "player-1")

        self.assertEqual(local_seek["canonicalUpdate"]["controlVersion"], 2)
        self.assertEqual(
            local_seek["canonicalUpdate"]["appliedControlVersion"],
            2,
        )
        self.assertEqual(context["version"], 2)
        self.assertEqual(context["queueRevision"], 1)
        self.assertEqual(context["controlVersion"], 2)
        self.assertEqual(context["currentIndex"], 0)
        self.assertEqual(context["positionMs"], 500)
        self.assertEqual(device["appliedControlVersion"], 2)
        self.assertEqual(device["positionMs"], 500)

    def test_stale_applied_feedback_returns_source_only_correction_without_mutation(self):
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
        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 100,
                "clientSeq": 1,
            },
            1000,
        )
        mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            1,
            position_ms=100,
            requesting_client_id="controller-1",
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="nonce-1",
            accepted_at_ms=1050,
            execution_timeout_ms=15000,
        )
        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "remoteCommand",
                "executionStatus": "committed",
                "commandControlVersion": 2,
                "appliedControlVersion": 2,
                "state": "paused",
                "trackId": "song-1",
                "positionMs": 100,
                "clientSeq": 2,
            },
            1075,
        )
        correction = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 0,
                "clientSeq": 3,
            },
            1100,
        )
        self.assertTrue(correction["sourceOnly"])
        self.assertEqual(correction["canonicalUpdate"]["positionMs"], 100)
        self.assertEqual(correction["canonicalUpdate"]["state"], "paused")

    def test_ensure_creates_idle_initializes_same_context_and_rebinds(self):
        idle_result = ensureStrictPlaybackContextState(
            "alice",
            "player-1",
            "device:player-1",
            [],
            None,
            0,
            "idle",
        )
        idle_context, mutated = idle_result
        context_id = idle_context["playbackContextId"]

        self.assertTrue(mutated)
        self.assertTrue(idle_result.binding_mutated)
        self.assertEqual(idle_context["state"], "idle")
        self.assertEqual(idle_context["queueSongIds"], [])
        self.assertNotIn("currentIndex", serializePlaybackContextV2(idle_context))
        self.assertNotIn("trackId", serializePlaybackContextV2(idle_context))

        initialized_result = ensureStrictPlaybackContextState(
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            250,
            "paused",
        )
        initialized, mutated = initialized_result

        self.assertTrue(mutated)
        self.assertFalse(initialized_result.binding_mutated)
        self.assertEqual(initialized["playbackContextId"], context_id)
        self.assertEqual(initialized["queueSongIds"], ["song-1"])
        self.assertEqual(initialized["state"], "paused")
        self.assertEqual(initialized["version"], 2)
        self.assertEqual(initialized["queueRevision"], 2)
        self.assertEqual(initialized["controlVersion"], 2)
        self.assertEqual(initialized["epoch"], 1)

        canonical_result = ensureStrictPlaybackContextState(
            "alice",
            "player-1",
            "device:player-1",
            ["different-song"],
            0,
            0,
            "stopped",
        )
        canonical, mutated = canonical_result
        self.assertFalse(mutated)
        self.assertEqual(canonical["queueSongIds"], ["song-1"])
        self.assertEqual(canonical["controlVersion"], 2)

        rebound_result = ensureStrictPlaybackContextState(
            "alice",
            "player-1",
            "device:player-2",
            [],
            None,
            0,
            "idle",
        )
        rebound, mutated = rebound_result

        self.assertTrue(mutated)
        self.assertTrue(rebound_result.binding_mutated)
        self.assertEqual(rebound["playbackContextId"], context_id)
        self.assertEqual(rebound["authorityDeviceSessionId"], "device:player-2")
        self.assertEqual(rebound["queueSongIds"], ["song-1"])
        self.assertEqual(rebound["epoch"], 2)
        self.assertEqual(rebound["version"], 3)
        self.assertEqual(rebound["queueRevision"], 2)
        self.assertEqual(rebound["controlVersion"], 3)
        self.assertEqual(len(rebound_result.affected_authority_pairs), 2)

    def test_ensure_fails_closed_when_stable_client_has_multiple_contexts(self):
        for context_id in ("context-1", "context-2"):
            createStrictPlaybackContextState(
                context_id,
                "alice",
                "player-1",
                "device:player-1",
                ["song-1"],
                0,
                0,
                "paused",
            )

        with self.assertRaises(PlaybackContextEnsureConflictError):
            ensureStrictPlaybackContextState(
                "alice",
                "player-1",
                "device:player-1",
                [],
                None,
                0,
                "idle",
            )

    def test_ensure_returns_exact_pair_when_legacy_active_contexts_remain(self):
        for context_id in (
            "device:player-1",
            "player-1",
        ):
            createStrictPlaybackContextState(
                context_id,
                "alice",
                "player-1",
                None,
                ["legacy-song"],
                0,
                0,
                "paused",
            )
        createStrictPlaybackContextState(
            "playback:canonical",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            250,
            "playing",
        )

        result = ensureStrictPlaybackContextState(
            "alice",
            "player-1",
            "device:player-1",
            [],
            None,
            0,
            "idle",
        )

        canonical, mutated = result
        self.assertFalse(mutated)
        self.assertFalse(result.binding_mutated)
        self.assertEqual(canonical["playbackContextId"], "playback:canonical")
        self.assertEqual(
            canonical["authorityDeviceSessionId"],
            "device:player-1",
        )
        self.assertNotEqual(
            canonical["playbackContextId"],
            canonical["authorityDeviceSessionId"],
        )
        self.assertEqual(canonical["queueSongIds"], ["song-1"])
        self.assertEqual(canonical["positionMs"], 250)

    def test_queue_sync_crosses_idle_boundary_with_closed_snapshot(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            500,
            "playing",
        )

        idle = mutateStrictPlaybackContextQueue(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            [],
            None,
            0,
            1,
            1,
        )
        self.assertEqual(idle["state"], "idle")
        self.assertEqual(idle["queueSongIds"], [])
        self.assertIsNone(idle["trackId"])
        self.assertEqual(idle["version"], 2)
        self.assertEqual(idle["queueRevision"], 2)
        self.assertEqual(idle["controlVersion"], 2)
        idle_device = getDevicePlaybackState("context-1", "player-1")
        self.assertEqual(idle_device["appliedControlVersion"], 2)
        self.assertEqual(idle_device["state"], "idle")

        queue_backed = mutateStrictPlaybackContextQueue(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-2"],
            0,
            0,
            2,
            2,
        )
        self.assertEqual(queue_backed["state"], "paused")
        self.assertEqual(queue_backed["trackId"], "song-2")
        self.assertEqual(queue_backed["version"], 3)
        self.assertEqual(queue_backed["queueRevision"], 3)
        self.assertEqual(queue_backed["controlVersion"], 3)
        queue_device = getDevicePlaybackState("context-1", "player-1")
        self.assertEqual(queue_device["appliedControlVersion"], 3)
        self.assertEqual(queue_device["state"], "paused")
        self.assertEqual(queue_device["trackId"], "song-2")

    def test_queue_sync_preserves_each_non_empty_context_state(self):
        for index, state_name in enumerate(
            ("playing", "paused", "stopped"),
            start=1,
        ):
            with self.subTest(state_name=state_name):
                context_id = "context-state-%d" % index
                client_id = "player-state-%d" % index
                device_session_id = "device:player-state-%d" % index
                createStrictPlaybackContextState(
                    context_id,
                    "alice",
                    client_id,
                    device_session_id,
                    ["song-1", "song-2"],
                    0,
                    100,
                    state_name,
                )

                updated = mutateStrictPlaybackContextQueue(
                    context_id,
                    "alice",
                    client_id,
                    device_session_id,
                    ["song-1", "song-2"],
                    1,
                    25,
                    1,
                    1,
                    position_sampled_at_server_ms=1100,
                )

                self.assertEqual(updated["state"], state_name)
                self.assertEqual(updated["trackId"], "song-2")
                self.assertEqual(updated["controlVersion"], 2)

    def test_queue_sync_advances_existing_authority_device_state_atomically(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2"],
            0,
            500,
            "playing",
        )
        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 500,
                "positionSampledAtServerMs": 900,
                "playbackRate": 1.25,
                "volume": 35,
                "muted": True,
                "clientSeq": 7,
            },
            1000,
        )

        updated = mutateStrictPlaybackContextQueue(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2"],
            1,
            25,
            1,
            1,
            position_sampled_at_server_ms=1100,
            connection_nonce="nonce-1",
        )

        device = getDevicePlaybackState("context-1", "player-1")
        self.assertEqual(updated["controlVersion"], 2)
        self.assertEqual(device["appliedControlVersion"], 2)
        self.assertEqual(device["trackId"], "song-2")
        self.assertEqual(device["state"], "playing")
        self.assertEqual(device["positionMs"], 25)
        self.assertEqual(device["positionSampledAtServerMs"], 1100)
        self.assertEqual(device["playbackRate"], 1.25)
        self.assertEqual(device["volume"], 35)
        self.assertIs(device["muted"], True)
        self.assertEqual(device["clientSeq"], 7)

        content_only = mutateStrictPlaybackContextQueue(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-3", "song-2"],
            1,
            25,
            2,
            position_sampled_at_server_ms=1200,
        )
        unchanged_device = getDevicePlaybackState("context-1", "player-1")
        self.assertEqual(content_only["controlVersion"], 2)
        self.assertEqual(unchanged_device["appliedControlVersion"], 2)
        self.assertEqual(
            unchanged_device["positionSampledAtServerMs"],
            1100,
        )

    def test_queue_sync_preserves_paused_and_stopped_device_facts(self):
        for index, state_name in enumerate(("paused", "stopped"), start=1):
            with self.subTest(state_name=state_name):
                context_id = "context-device-%d" % index
                client_id = "player-device-%d" % index
                device_session_id = "device:player-device-%d" % index
                nonce = "nonce-device-%d" % index
                createStrictPlaybackContextState(
                    context_id,
                    "alice",
                    client_id,
                    device_session_id,
                    ["song-1", "song-2"],
                    0,
                    500,
                    "playing",
                )
                applyStrictPlaybackUpdate(
                    context_id,
                    "alice",
                    client_id,
                    device_session_id,
                    nonce,
                    {
                        "playbackContextId": context_id,
                        "deviceSessionId": device_session_id,
                        "origin": "passive",
                        "appliedControlVersion": 1,
                        "state": state_name,
                        "trackId": "song-1",
                        "positionMs": 500,
                        "positionSampledAtServerMs": 900,
                        "playbackRate": 1.25 + index / 10,
                        "volume": 20 + index * 10,
                        "muted": index == 1,
                        "clientSeq": 4,
                    },
                    1000,
                )

                updated = mutateStrictPlaybackContextQueue(
                    context_id,
                    "alice",
                    client_id,
                    device_session_id,
                    ["song-1", "song-2"],
                    1,
                    25,
                    1,
                    1,
                    position_sampled_at_server_ms=1100,
                    connection_nonce=nonce,
                )
                device = getDevicePlaybackState(context_id, client_id)

                self.assertEqual(updated["state"], "playing")
                self.assertEqual(updated["controlVersion"], 2)
                self.assertEqual(device["appliedControlVersion"], 2)
                self.assertEqual(device["state"], state_name)
                self.assertEqual(device["trackId"], "song-2")
                self.assertEqual(device["positionMs"], 25)
                self.assertEqual(
                    device["positionSampledAtServerMs"],
                    1100,
                )
                self.assertEqual(
                    device["playbackRate"],
                    1.25 + index / 10,
                )
                self.assertEqual(device["volume"], 20 + index * 10)
                self.assertEqual(device["muted"], index == 1)
                self.assertEqual(device["clientSeq"], 4)

    def test_equal_applied_passive_update_keeps_context_and_device_independent(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            100,
            "paused",
        )
        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "paused",
                "trackId": "song-1",
                "positionMs": 100,
                "positionSampledAtServerMs": 900,
                "playbackRate": 1.0,
                "clientSeq": 1,
            },
            1000,
        )
        before = getPlaybackContextState("context-1")

        result = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 250,
                "positionSampledAtServerMs": 1100,
                "playbackRate": 1.5,
                "clientSeq": 2,
            },
            1200,
        )
        after = getPlaybackContextState("context-1")
        device = getDevicePlaybackState("context-1", "player-1")

        self.assertTrue(result["created"])
        self.assertEqual(
            tuple(
                before[field]
                for field in (
                    "state",
                    "version",
                    "queueRevision",
                    "controlVersion",
                )
            ),
            tuple(
                after[field]
                for field in (
                    "state",
                    "version",
                    "queueRevision",
                    "controlVersion",
                )
            ),
        )
        self.assertEqual(after["state"], "paused")
        self.assertEqual(device["state"], "playing")
        self.assertEqual(device["positionMs"], 250)
        self.assertEqual(device["playbackRate"], 1.5)
        self.assertEqual(device["appliedControlVersion"], 1)

    def test_passive_update_cannot_advance_existing_applied_cursor(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            100,
            "playing",
        )
        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 100,
                "positionSampledAtServerMs": 900,
                "playbackRate": 1.0,
                "clientSeq": 1,
            },
            1000,
        )
        mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            1,
            requesting_client_id="controller-1",
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="nonce-1",
            accepted_at_ms=1100,
            execution_timeout_ms=15000,
        )
        before_context = getPlaybackContextState("context-1")
        before_device = getDevicePlaybackState("context-1", "player-1")

        with self.assertRaises(PlaybackControlTransactionConflictError):
            applyStrictPlaybackUpdate(
                "context-1",
                "alice",
                "player-1",
                "device:player-1",
                "nonce-1",
                {
                    "playbackContextId": "context-1",
                    "deviceSessionId": "device:player-1",
                    "origin": "passive",
                    "appliedControlVersion": 2,
                    "state": "paused",
                    "trackId": "song-1",
                    "positionMs": 100,
                    "positionSampledAtServerMs": 1200,
                    "playbackRate": 1.0,
                    "clientSeq": 2,
                },
                1300,
            )

        self.assertEqual(
            getPlaybackContextState("context-1"),
            before_context,
        )
        self.assertEqual(
            getDevicePlaybackState("context-1", "player-1"),
            before_device,
        )
        transaction = getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(transaction["status"], "pending")
        self.assertEqual(before_context["controlVersion"], 2)
        self.assertEqual(before_device["appliedControlVersion"], 1)

    def test_queue_sync_without_feedback_creates_hidden_applied_baseline(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2"],
            0,
            0,
            "playing",
        )

        updated = mutateStrictPlaybackContextQueue(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2"],
            1,
            25,
            1,
            1,
            position_sampled_at_server_ms=1100,
        )

        baseline = getDevicePlaybackState("context-1", "player-1")
        self.assertEqual(updated["controlVersion"], 2)
        self.assertEqual(baseline["appliedControlVersion"], 2)
        self.assertEqual(baseline["clientSeq"], 0)
        self.assertEqual(getDevicePlaybackStates("context-1"), [])

        stale_payload = {
            "playbackContextId": "context-1",
            "deviceSessionId": "device:player-1",
            "origin": "passive",
            "appliedControlVersion": 1,
            "state": "playing",
            "trackId": "song-1",
            "positionMs": 0,
            "positionSampledAtServerMs": 1200,
            "playbackRate": 1.0,
            "clientSeq": 1,
        }
        stale = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            stale_payload,
            1300,
        )
        self.assertFalse(stale["created"])
        self.assertEqual(stale["canonicalUpdate"]["clientSeq"], 1)
        self.assertEqual(
            stale["canonicalUpdate"]["appliedControlVersion"],
            2,
        )
        self.assertEqual(
            getDevicePlaybackState("context-1", "player-1")["clientSeq"],
            0,
        )
        self.assertEqual(getDevicePlaybackStates("context-1"), [])

        replay = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            stale_payload,
            1300,
        )
        self.assertFalse(replay["created"])
        self.assertEqual(
            replay["canonicalUpdate"],
            stale["canonicalUpdate"],
        )
        conflicting_stale = dict(stale_payload)
        conflicting_stale["positionMs"] = 1
        with self.assertRaises(PlaybackClientSequenceConflictError):
            applyStrictPlaybackUpdate(
                "context-1",
                "alice",
                "player-1",
                "device:player-1",
                "nonce-1",
                conflicting_stale,
                1350,
            )

        accepted = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 2,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 50,
                "positionSampledAtServerMs": 1400,
                "playbackRate": 1.0,
                "clientSeq": 2,
            },
            1500,
        )
        self.assertTrue(accepted["created"])
        self.assertEqual(
            getDevicePlaybackStates("context-1")[0]["appliedControlVersion"],
            2,
        )
        self.assertEqual(
            getDevicePlaybackStates("context-1")[0]["clientSeq"],
            2,
        )

    def test_queue_sync_preserves_pending_gap_and_rejects_control_change(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2"],
            0,
            0,
            "playing",
        )
        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 0,
                "clientSeq": 1,
            },
            1000,
        )
        mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            1,
            requesting_client_id="controller-1",
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="nonce-1",
            accepted_at_ms=1100,
            execution_timeout_ms=15000,
        )
        before = getPlaybackContextState("context-1")

        content_only = mutateStrictPlaybackContextQueue(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2", "song-3"],
            0,
            50,
            before["queueRevision"],
            position_sampled_at_server_ms=1200,
            connection_nonce="nonce-1",
        )
        pending_device = getDevicePlaybackState("context-1", "player-1")

        self.assertEqual(content_only["version"], before["version"] + 1)
        self.assertEqual(
            content_only["queueRevision"],
            before["queueRevision"] + 1,
        )
        self.assertEqual(content_only["controlVersion"], 2)
        self.assertEqual(pending_device["appliedControlVersion"], 1)
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2)["status"],
            "pending",
        )

        before_control_change = getPlaybackContextState("context-1")
        before_control_device = getDevicePlaybackState("context-1", "player-1")

        with self.assertRaises(PlaybackControlTransactionConflictError):
            mutateStrictPlaybackContextQueue(
                "context-1",
                "alice",
                "player-1",
                "device:player-1",
                ["song-1", "song-2", "song-3"],
                1,
                0,
                before_control_change["queueRevision"],
                before_control_change["controlVersion"],
                position_sampled_at_server_ms=1250,
                connection_nonce="nonce-1",
            )

        after = getPlaybackContextState("context-1")
        self.assertEqual(after, before_control_change)
        self.assertEqual(
            getDevicePlaybackState("context-1", "player-1"),
            before_control_device,
        )
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2)["status"],
            "pending",
        )

        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "remoteCommand",
                "executionStatus": "failed",
                "commandControlVersion": 2,
                "appliedControlVersion": 1,
                "errorCode": "playback_failed",
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 0,
                "clientSeq": 2,
            },
            1300,
        )
        reconciled = getPlaybackContextState("context-1")
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2)["status"],
            "failed",
        )
        advanced = mutateStrictPlaybackContextQueue(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2", "song-3"],
            1,
            50,
            reconciled["queueRevision"],
            reconciled["controlVersion"],
            position_sampled_at_server_ms=1400,
            connection_nonce="nonce-1",
        )
        self.assertEqual(advanced["controlVersion"], 3)
        self.assertEqual(
            getDevicePlaybackState("context-1", "player-1")[
                "appliedControlVersion"
            ],
            3,
        )

    def test_queue_sync_rolls_back_context_and_device_state(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2"],
            0,
            0,
            "playing",
        )
        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 0,
                "positionSampledAtServerMs": 900,
                "playbackRate": 1.5,
                "clientSeq": 3,
            },
            1000,
        )
        before_context = getPlaybackContextState("context-1")
        before_device = getDevicePlaybackState("context-1", "player-1")

        def fail_after_device_update(*_args):
            raise RuntimeError("injected rollback")

        with self.assertRaisesRegex(RuntimeError, "injected rollback"):
            mutateStrictPlaybackContextQueue(
                "context-1",
                "alice",
                "player-1",
                "device:player-1",
                ["song-1", "song-2"],
                1,
                25,
                1,
                1,
                position_sampled_at_server_ms=1100,
                connection_nonce="nonce-1",
                post_mutation_hook=fail_after_device_update,
            )

        self.assertEqual(getPlaybackContextState("context-1"), before_context)
        self.assertEqual(
            getDevicePlaybackState("context-1", "player-1"),
            before_device,
        )

    def test_prepare_transaction_enforces_one_active_intent_and_terminal_replay(self):
        request_payload = {
            "initialQueue": {
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "positionMs": 0,
            }
        }
        prepare, created = createPlaybackPrepareTransaction(
            "context-1",
            "alice",
            1,
            "intent-1",
            "controller-1",
            "player-1",
            "device:player-1",
            "nonce-1",
            1,
            request_payload,
            1,
            11000,
        )
        self.assertTrue(created)
        self.assertEqual(prepare["status"], "preparing")
        self.assertEqual(
            getPlaybackPrepareTransaction("context-1", 1, "intent-1"),
            prepare,
        )
        self.assertEqual(listExpiredPlaybackPrepareTransactions(10999), [])
        self.assertEqual(
            [item["intentId"] for item in listExpiredPlaybackPrepareTransactions(11000)],
            ["intent-1"],
        )

        replay, created = createPlaybackPrepareTransaction(
            "context-1",
            "alice",
            1,
            "intent-1",
            "controller-1",
            "player-1",
            "device:player-1",
            "nonce-1",
            1,
            request_payload,
            1,
            11000,
        )
        self.assertFalse(created)
        self.assertEqual(replay, prepare)

        with self.assertRaises(PlaybackPrepareTransactionConflictError):
            createPlaybackPrepareTransaction(
                "context-1",
                "alice",
                1,
                "intent-1",
                "controller-1",
                "player-1",
                "device:player-1",
                "nonce-1",
                1,
                {},
                1,
                11000,
            )

        with self.assertRaises(PlaybackPrepareAlreadyActiveError):
            createPlaybackPrepareTransaction(
                "context-1",
                "alice",
                1,
                "intent-2",
                "controller-1",
                "player-1",
                "device:player-1",
                "nonce-1",
                1,
                {},
                1,
                11000,
            )

        terminal, changed = settlePlaybackPrepareTransaction(
            "context-1",
            1,
            "intent-1",
            "ready",
            {
                "playbackContextId": "context-1",
                "intentId": "intent-1",
                "ready": True,
                "controlVersion": 1,
            },
            10500,
        )
        self.assertTrue(changed)
        self.assertEqual(terminal["status"], "ready")

        replay, changed = settlePlaybackPrepareTransaction(
            "context-1",
            1,
            "intent-1",
            "ready",
            terminal["canonicalResult"],
            10500,
        )
        self.assertFalse(changed)
        self.assertEqual(replay, terminal)

        with self.assertRaises(PlaybackPrepareTransactionConflictError):
            settlePlaybackPrepareTransaction(
                "context-1",
                1,
                "intent-1",
                "failed",
                {"ready": False},
                10501,
                error_code="prepare_timeout",
            )

    def test_local_intent_replays_first_canonical_result_and_rejects_conflict(self):
        request_payload = {
            "intentId": "local-1",
            "queueIndex": 1,
            "trackId": "song-2",
        }
        canonical_update = {
            "origin": "localUser",
            "controlVersion": 3,
            "appliedControlVersion": 3,
        }
        intent, created = savePlaybackLocalIntent(
            "context-1",
            "alice",
            1,
            "local-1",
            "player-1",
            "device:player-1",
            request_payload,
            canonical_update,
            3,
            2,
        )
        self.assertTrue(created)
        self.assertEqual(intent["canonicalUpdate"], canonical_update)

        replay, created = savePlaybackLocalIntent(
            "context-1",
            "alice",
            1,
            "local-1",
            "player-1",
            "device:player-1",
            request_payload,
            {"ignored": "later-result"},
            4,
            3,
        )
        self.assertFalse(created)
        self.assertEqual(replay, intent)

        with self.assertRaises(PlaybackLocalIntentConflictError):
            savePlaybackLocalIntent(
                "context-1",
                "alice",
                1,
                "local-1",
                "player-1",
                "device:player-1",
                dict(request_payload, queueIndex=0),
                canonical_update,
                3,
                2,
            )

    def test_control_mutation_pre_validator_runs_before_canonical_and_transaction_writes(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            25,
            "playing",
        )
        events = []
        observed = []

        def validator(current):
            events.append("pre_mutation_validator")
            observed.append(dict(current))
            self.assertEqual(current["state"], "playing")
            self.assertEqual(current["version"], 1)
            self.assertEqual(current["controlVersion"], 1)
            self.assertEqual(current["queueRevision"], 1)
            self.assertEqual(
                db.EmoPlaybackControlTransaction.select().count(),
                0,
            )

        def post_hook(record, result, current):
            events.append("canonical mutation")
            self.assertEqual(current["state"], "playing")
            self.assertEqual(record.state, "paused")
            self.assertEqual(result["state"], "paused")
            transaction = db.EmoPlaybackControlTransaction.get_or_none(
                (
                    db.EmoPlaybackControlTransaction.playback_context_id
                    == "context-1"
                )
                & (db.EmoPlaybackControlTransaction.epoch == 1)
                & (
                    db.EmoPlaybackControlTransaction.command_control_version
                    == 2
                )
            )
            self.assertIsNotNone(transaction)
            events.append("transaction creation")
            events.append("post_mutation_hook")
            return {"hook": True}

        updated = mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            1,
            requesting_client_id="controller-1",
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="authority-nonce-1",
            accepted_at_ms=1000,
            execution_timeout_ms=15000,
            requesting_device_session_id="device:controller-1",
            requesting_connection_nonce="requester-nonce-1",
            requesting_connection_epoch=1,
            pre_mutation_validator=validator,
            post_mutation_hook=post_hook,
        )

        self.assertEqual(
            events,
            [
                "pre_mutation_validator",
                "canonical mutation",
                "transaction creation",
                "post_mutation_hook",
            ],
        )
        self.assertEqual(observed[0]["state"], "playing")
        self.assertEqual(updated["state"], "paused")
        self.assertEqual(updated["controlVersion"], 2)
        self.assertEqual(updated["_broadcastMutation"], {"hook": True})

    def test_control_mutation_pre_validator_receives_an_isolated_context_copy(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            25,
            "playing",
        )

        def validator(current):
            current["state"] = "corrupted"
            current["queueSongIds"].append("unpersisted-song")

        updated = mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            1,
            pre_mutation_validator=validator,
        )

        self.assertEqual(updated["state"], "paused")
        self.assertEqual(updated["queueSongIds"], ["song-1"])
        self.assertEqual(getPlaybackContextState("context-1")["state"], "paused")
        self.assertEqual(
            getPlaybackContextState("context-1")["queueSongIds"],
            ["song-1"],
        )

    def test_control_mutation_pre_validator_failure_rolls_back_everything(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            25,
            "playing",
        )
        before = getPlaybackContextState("context-1")
        post_hook = mock.Mock()

        class ValidatorFailure(Exception):
            pass

        def validator(current):
            self.assertEqual(current["state"], "playing")
            raise ValidatorFailure("physical generation changed")

        with self.assertRaises(ValidatorFailure) as failure:
            mutateStrictPlaybackContextControl(
                "context-1",
                "alice",
                "controller-1",
                "player.pause",
                1,
                requesting_client_id="controller-1",
                authority_client_id="player-1",
                authority_device_session_id="device:player-1",
                routed_connection_nonce="authority-nonce-1",
                accepted_at_ms=1000,
                execution_timeout_ms=15000,
                requesting_device_session_id="device:controller-1",
                requesting_connection_nonce="requester-nonce-1",
                requesting_connection_epoch=1,
                pre_mutation_validator=validator,
                post_mutation_hook=post_hook,
            )

        self.assertEqual(str(failure.exception), "physical generation changed")
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertEqual(
            db.EmoPlaybackControlTransaction.select().count(),
            0,
        )
        post_hook.assert_not_called()

    def test_control_mutation_static_validation_short_circuits_pre_validator(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2"],
            0,
            25,
            "playing",
        )
        before = getPlaybackContextState("context-1")
        validator = mock.Mock()

        with self.assertRaises(PlaybackContextStaleVersionError):
            mutateStrictPlaybackContextControl(
                "context-1",
                "alice",
                "controller-1",
                "player.pause",
                99,
                pre_mutation_validator=validator,
            )
        with self.assertRaises(PlaybackContextStaleVersionError):
            mutateStrictPlaybackContextControl(
                "context-1",
                "alice",
                "controller-1",
                "queue.playItem",
                1,
                base_queue_revision=99,
                current_index=1,
                pre_mutation_validator=validator,
            )

        validator.assert_not_called()
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertEqual(
            db.EmoPlaybackControlTransaction.select().count(),
            0,
        )

    def test_control_mutation_pre_validator_can_match_physical_generation_and_reject_replacement(self):
        physical_state = WebSocketState()
        physical_state.register_session("sid-old", now=100)
        physical_state.authenticate_session("sid-old", "alice")
        physical_state.register_client(
            "sid-old",
            "player-1",
            {
                "userName": "alice",
                "deviceSessionId": "device:player-1",
                "roles": ["player"],
            },
            now=100,
        )
        old_generation = physical_state.get_current_physical_generation(
            "alice",
            "player-1",
            "device:player-1",
        )
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            25,
            "playing",
        )

        def matching_validator(current):
            self.assertTrue(
                physical_state.matches_current_physical_generation(
                    old_generation["userName"],
                    old_generation["clientId"],
                    old_generation["deviceSessionId"],
                    old_generation["sid"],
                    old_generation["connectionNonce"],
                    old_generation["connectionEpoch"],
                )
            )
            self.assertEqual(current["state"], "playing")

        updated = mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            1,
            pre_mutation_validator=matching_validator,
        )
        self.assertEqual(updated["state"], "paused")

        physical_state.register_session("sid-new", now=101)
        physical_state.authenticate_session("sid-new", "alice")
        physical_state.register_client(
            "sid-new",
            "player-1",
            {
                "userName": "alice",
                "deviceSessionId": "device:player-1",
                "roles": ["player"],
            },
            now=101,
        )
        before = getPlaybackContextState("context-1")

        class ReplacedGeneration(Exception):
            pass

        def replaced_validator(current):
            self.assertFalse(
                physical_state.matches_current_physical_generation(
                    old_generation["userName"],
                    old_generation["clientId"],
                    old_generation["deviceSessionId"],
                    old_generation["sid"],
                    old_generation["connectionNonce"],
                    old_generation["connectionEpoch"],
                )
            )
            raise ReplacedGeneration("requester generation replaced")

        with self.assertRaises(ReplacedGeneration):
            mutateStrictPlaybackContextControl(
                "context-1",
                "alice",
                "controller-1",
                "player.play",
                before["controlVersion"],
                pre_mutation_validator=replaced_validator,
            )
        self.assertEqual(getPlaybackContextState("context-1"), before)

    def test_control_mutation_without_pre_validator_remains_compatible(self):
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
        updated = mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            1,
        )
        self.assertEqual(updated["state"], "paused")
        self.assertEqual(updated["controlVersion"], 2)
