import concurrent.futures
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from supysonic import db
from supysonic.emo import ws_store
from supysonic.emo.ws_state import WebSocketState
from supysonic.emo.ws_store import (
    PlaybackContextAuthorityAmbiguousError,
    PlaybackContextBroadcastBarrierError,
    PlaybackContextCloseConflictError,
    PlaybackContextCloseInvariantError,
    PlaybackContextClosedError,
    PlaybackContextEnsureConflictError,
    PlaybackContextHandoffBarrierError,
    PlaybackContextIntentConflictError,
    PlaybackContextRestoreInProgressError,
    PlaybackContextStaleVersionError,
    PlaybackControlTransactionConflictError,
    PlaybackControlReconciliationConflictError,
    PlaybackPassiveAppliedVersionConflictError,
    PlaybackHandoffTargetConflictError,
    PlaybackLocalIntentConflictError,
    PlaybackPrepareAlreadyActiveError,
    PlaybackPrepareTransactionConflictError,
    PlaybackClientSequenceConflictError,
    closeStrictPlaybackContextState,
    cleanupCoreStartupRecoveryRetention,
    cleanupStrictPlaybackContextRetention,
    applyStrictPlaybackUpdate,
    commitStrictPlaybackHandoff,
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
    getPlaybackContextStateForUser,
    getPlaybackContextWithDeviceStates,
    getPlaybackControlTransaction,
    getPlaybackControlReconciliation,
    getCoreStartupRecovery,
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
    listCoreStartupRecoveries,
    listExpiredPlaybackControlTransactions,
    listPlaybackControlReconciliations,
    listExpiredPlaybackPrepareTransactions,
    listPendingPlaybackControlTransactions,
    listPendingPlaybackControlTransactionsForAuthorityConnection,
    listUserPlaybackContexts,
    listPlaybackContexts,
    mutateStrictPlaybackContextControl,
    mutateStrictPlaybackContextQueue,
    recoverPendingPlaybackControlsForStartup,
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
    markStrictPlaybackHandoffCommitEnqueued,
)


class EmoWebSocketStoreTestCase(unittest.TestCase):
    RETENTION_NOW_MS = 1800000000000

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp()
        os.close(handle)
        db.init_database("sqlite:///" + self.db_path)

    def tearDown(self):
        db.release_database()
        os.remove(self.db_path)

    def _complete_exact_handoff(
        self,
        playback_context_id,
        handoff_id,
        user_name,
        target_client_id,
        target_device_session_id,
        position_ms=None,
        proof_overrides=None,
    ):
        handoff = getPlaybackHandoff(handoff_id)
        snapshot = handoff["snapshot"]
        proof = {
            "queue_index": snapshot["currentIndex"],
            "track_id": snapshot["trackId"],
            "state": "playing",
            "position_ms": (
                snapshot["positionMs"]
                if position_ms is None
                else position_ms
            ),
            "position_sampled_at_server_ms": snapshot[
                "effectiveAtServerMs"
            ],
            "playback_rate": snapshot["playbackRate"],
            "applied_control_version": handoff["controlVersion"],
            "client_seq": 1,
            "server_received_at_ms": snapshot["effectiveAtServerMs"],
        }
        proof.update(proof_overrides or {})
        return completeStrictPlaybackHandoff(
            playback_context_id,
            handoff_id,
            user_name,
            target_client_id,
            target_device_session_id,
            **proof,
            expected_source_client_id=handoff["sourceClientId"],
            expected_source_device_session_id=(
                handoff["sourceDeviceSessionId"]
            ),
            expected_source_connection_nonce=(
                handoff["sourceConnectionNonce"]
            ),
            expected_source_connection_epoch=(
                handoff["sourceConnectionEpoch"]
            ),
            expected_target_connection_nonce=(
                handoff["targetConnectionNonce"]
            ),
            expected_target_connection_epoch=(
                handoff["targetConnectionEpoch"]
            ),
        )

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
        deterministic_dependency_admission=False,
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
            deterministic_dependency_admission=(
                deterministic_dependency_admission
            ),
        )

    def _create_retention_context(self, playback_context_id="retention-context"):
        return createStrictPlaybackContextState(
            playback_context_id,
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            0,
            "playing",
        )

    def _create_exact_handoff_source(
        self,
        playback_context_id="handoff-source-context",
        handoff_id="handoff-source-1",
        context_state="playing",
        source_client_id="source-player",
        target_client_id="target-player",
    ):
        source_device_session_id = "device:%s" % source_client_id
        target_device_session_id = "device:%s" % target_client_id
        source_connection_nonce = "source-nonce-%s" % handoff_id
        createStrictPlaybackContextState(
            playback_context_id,
            "alice",
            source_client_id,
            source_device_session_id,
            ["song-1"],
            0,
            100,
            context_state,
        )
        applyStrictPlaybackUpdate(
            playback_context_id,
            "alice",
            source_client_id,
            source_device_session_id,
            source_connection_nonce,
            {
                "playbackContextId": playback_context_id,
                "deviceSessionId": source_device_session_id,
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
        handoff, created = createStrictPlaybackHandoff(
            playback_context_id,
            {
                "handoffId": handoff_id,
                "requestId": handoff_id + "-request",
                "playbackContextId": playback_context_id,
                "userName": "alice",
                "sourceClientId": source_client_id,
                "sourceDeviceSessionId": source_device_session_id,
                "sourceConnectionNonce": source_connection_nonce,
                "sourceConnectionEpoch": 1,
                "targetClientId": target_client_id,
                "targetDeviceSessionId": target_device_session_id,
                "targetConnectionNonce": "target-nonce-%s" % handoff_id,
                "targetConnectionEpoch": 1,
                "originClientId": "controller-1",
                "status": "preparing",
                "baseControlVersion": 1,
                "controlVersion": 2,
                "prepareId": handoff_id + "-prepare",
                "snapshot": {},
            },
            target_device_session_id,
        )
        self.assertTrue(created)
        return handoff

    def _commit_exact_handoff_source(
        self,
        playback_context_id,
        handoff_id,
        effective_at_server_ms=1000,
        track_duration_ms=None,
    ):
        source_client_id = "source-%s" % handoff_id
        target_client_id = "target-%s" % handoff_id
        self._create_exact_handoff_source(
            playback_context_id=playback_context_id,
            handoff_id=handoff_id,
            source_client_id=source_client_id,
            target_client_id=target_client_id,
        )
        committing, transitioned = commitStrictPlaybackHandoff(
            playback_context_id,
            handoff_id,
            "alice",
            effective_at_server_ms + 5000,
            effective_at_server_ms,
            track_duration_ms,
        )
        self.assertTrue(transitioned)
        if committing["status"] == "failed":
            return committing
        committed, transitioned = markStrictPlaybackHandoffCommitEnqueued(
            playback_context_id,
            handoff_id,
            "alice",
        )
        self.assertTrue(transitioned)
        self.assertEqual(committed["status"], "committed")
        return committed

    def test_handoff_commit_separates_canonical_and_source_actual_state(self):
        handoff = self._create_exact_handoff_source(
            handoff_id="handoff-canonical-actual-split",
            context_state="paused",
        )

        self.assertEqual(handoff["snapshot"]["sourceContextState"], "paused")
        self.assertEqual(handoff["snapshot"]["state"], "playing")
        committing, transitioned = commitStrictPlaybackHandoff(
            handoff["playbackContextId"],
            handoff["handoffId"],
            handoff["userName"],
            9000,
            1200,
            None,
        )

        self.assertTrue(transitioned)
        self.assertEqual(committing["status"], "committing")
        context = getPlaybackContextState(handoff["playbackContextId"])
        self.assertEqual(context["state"], "paused")
        self.assertEqual(context["controlVersion"], 1)
        self.assertEqual(
            getDevicePlaybackState(
                handoff["playbackContextId"],
                handoff["sourceClientId"],
            )["state"],
            "playing",
        )

    def test_handoff_provisional_lane_commit_and_enqueue_are_isolated(self):
        handoff = self._create_exact_handoff_source(
            handoff_id="handoff-provisional-isolated",
        )
        context_before = getPlaybackContextState(
            handoff["playbackContextId"]
        )
        device_before = getDevicePlaybackState(
            handoff["playbackContextId"],
            handoff["sourceClientId"],
        )
        self.assertEqual(handoff["contextEpoch"], 1)
        self.assertEqual(handoff["baseControlVersion"], 1)
        self.assertEqual(handoff["controlVersion"], 2)
        self.assertEqual(
            handoff["snapshot"]["handoffControlVersion"],
            2,
        )

        committing, transitioned = commitStrictPlaybackHandoff(
            handoff["playbackContextId"],
            handoff["handoffId"],
            handoff["userName"],
            9000,
            1200,
            None,
        )

        self.assertTrue(transitioned)
        self.assertEqual(committing["status"], "committing")
        self.assertEqual(committing["completeExpiresAtMs"], 9000)
        self.assertEqual(
            getPlaybackContextState(handoff["playbackContextId"]),
            context_before,
        )
        self.assertEqual(
            getDevicePlaybackState(
                handoff["playbackContextId"],
                handoff["sourceClientId"],
            ),
            device_before,
        )
        self.assertEqual(
            db.EmoPlaybackControlTransaction.select().count(),
            0,
        )

        committing_replay, transitioned = commitStrictPlaybackHandoff(
            handoff["playbackContextId"],
            handoff["handoffId"],
            handoff["userName"],
            9000,
            1200,
            None,
        )
        self.assertFalse(transitioned)
        self.assertEqual(committing_replay, committing)

        committed, transitioned = markStrictPlaybackHandoffCommitEnqueued(
            handoff["playbackContextId"],
            handoff["handoffId"],
            handoff["userName"],
        )
        self.assertTrue(transitioned)
        self.assertEqual(committed["status"], "committed")
        replay, transitioned = markStrictPlaybackHandoffCommitEnqueued(
            handoff["playbackContextId"],
            handoff["handoffId"],
            handoff["userName"],
        )
        self.assertFalse(transitioned)
        self.assertEqual(replay, committed)
        self.assertEqual(
            getPlaybackContextState(handoff["playbackContextId"]),
            context_before,
        )
        self.assertEqual(
            db.EmoPlaybackControlTransaction.select().count(),
            0,
        )

    def test_handoff_complete_accepts_exact_time_and_position_boundaries(self):
        cases = (
            ("future", 1050, 1000, 250),
            ("age", 1000, 3000, 200),
            ("late", 2000, 2000, 1200),
            ("position", 1000, 1000, 1200),
        )
        for name, sampled_at_ms, received_at_ms, position_ms in cases:
            with self.subTest(boundary=name):
                context_id = "handoff-proof-boundary-%s" % name
                handoff_id = "handoff-proof-boundary-%s" % name
                handoff = self._commit_exact_handoff_source(
                    context_id,
                    handoff_id,
                )

                completed = self._complete_exact_handoff(
                    context_id,
                    handoff_id,
                    "alice",
                    handoff["targetClientId"],
                    handoff["targetDeviceSessionId"],
                    position_ms=position_ms,
                    proof_overrides={
                        "position_sampled_at_server_ms": sampled_at_ms,
                        "server_received_at_ms": received_at_ms,
                    },
                )

                self.assertTrue(completed.mutated)
                self.assertEqual(completed[0]["positionMs"], position_ms)
                self.assertEqual(completed[0]["controlVersion"], 2)
                self.assertEqual(completed[0]["epoch"], 2)

    def test_handoff_complete_rejects_just_over_proof_boundaries_without_writes(self):
        cases = (
            ("future", 1051, 1000, 251),
            ("age", 1000, 3001, 200),
            ("late", 2001, 2001, 1201),
            ("position", 1000, 1000, 1201),
        )
        for name, sampled_at_ms, received_at_ms, position_ms in cases:
            with self.subTest(boundary=name):
                context_id = "handoff-proof-reject-%s" % name
                handoff_id = "handoff-proof-reject-%s" % name
                handoff = self._commit_exact_handoff_source(
                    context_id,
                    handoff_id,
                )
                before_context = getPlaybackContextState(context_id)
                before_handoff = getPlaybackHandoff(handoff_id)
                before_states = getDevicePlaybackStates(context_id)

                with self.assertRaises(PlaybackHandoffTargetConflictError):
                    self._complete_exact_handoff(
                        context_id,
                        handoff_id,
                        "alice",
                        handoff["targetClientId"],
                        handoff["targetDeviceSessionId"],
                        position_ms=position_ms,
                        proof_overrides={
                            "position_sampled_at_server_ms": sampled_at_ms,
                            "server_received_at_ms": received_at_ms,
                        },
                    )

                self.assertEqual(getPlaybackContextState(context_id), before_context)
                self.assertEqual(getPlaybackHandoff(handoff_id), before_handoff)
                self.assertEqual(getDevicePlaybackStates(context_id), before_states)

    def test_handoff_complete_rejects_mismatched_actual_proof_without_writes(self):
        cases = (
            ("queue_index", {"queue_index": 1}),
            ("track_id", {"track_id": "song-other"}),
            ("state", {"state": "paused"}),
            ("playback_rate", {"playback_rate": 0.5}),
            ("applied", {"applied_control_version": 3}),
        )
        for name, overrides in cases:
            with self.subTest(field=name):
                context_id = "handoff-proof-field-%s" % name
                handoff_id = "handoff-proof-field-%s" % name
                handoff = self._commit_exact_handoff_source(
                    context_id,
                    handoff_id,
                )
                before_context = getPlaybackContextState(context_id)
                before_handoff = getPlaybackHandoff(handoff_id)
                before_states = getDevicePlaybackStates(context_id)

                with self.assertRaises(
                    (ValueError, PlaybackHandoffTargetConflictError)
                ):
                    self._complete_exact_handoff(
                        context_id,
                        handoff_id,
                        "alice",
                        handoff["targetClientId"],
                        handoff["targetDeviceSessionId"],
                        proof_overrides=overrides,
                    )

                self.assertEqual(getPlaybackContextState(context_id), before_context)
                self.assertEqual(getPlaybackHandoff(handoff_id), before_handoff)
                self.assertEqual(getDevicePlaybackStates(context_id), before_states)

    def test_handoff_complete_enforces_known_duration_and_commit_fail_fast(self):
        context_id = "handoff-known-duration-success"
        handoff_id = "handoff-known-duration-success"
        handoff = self._commit_exact_handoff_source(
            context_id,
            handoff_id,
            track_duration_ms=1200,
        )
        completed = self._complete_exact_handoff(
            context_id,
            handoff_id,
            "alice",
            handoff["targetClientId"],
            handoff["targetDeviceSessionId"],
            position_ms=1200,
            proof_overrides={
                "position_sampled_at_server_ms": 2000,
                "server_received_at_ms": 2000,
            },
        )
        self.assertTrue(completed.mutated)
        self.assertEqual(completed[0]["positionMs"], 1200)

        reject_context_id = "handoff-known-duration-reject"
        reject_handoff_id = "handoff-known-duration-reject"
        reject_handoff = self._commit_exact_handoff_source(
            reject_context_id,
            reject_handoff_id,
            track_duration_ms=1200,
        )
        before_context = getPlaybackContextState(reject_context_id)
        with self.assertRaises(PlaybackHandoffTargetConflictError):
            self._complete_exact_handoff(
                reject_context_id,
                reject_handoff_id,
                "alice",
                reject_handoff["targetClientId"],
                reject_handoff["targetDeviceSessionId"],
                position_ms=1201,
                proof_overrides={
                    "position_sampled_at_server_ms": 2000,
                    "server_received_at_ms": 2000,
                },
            )
        self.assertEqual(getPlaybackContextState(reject_context_id), before_context)

        fail_context_id = "handoff-known-duration-fail-fast"
        fail_handoff_id = "handoff-known-duration-fail-fast"
        failed = self._commit_exact_handoff_source(
            fail_context_id,
            fail_handoff_id,
            track_duration_ms=200,
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["errorCode"], "source_changed")
        self.assertEqual(
            getPlaybackContextState(fail_context_id)["authorityClientId"],
            failed["sourceClientId"],
        )

    def test_handoff_complete_consumes_target_ordinary_client_sequence(self):
        context_id = "handoff-complete-sequence"
        handoff_id = "handoff-complete-sequence"
        handoff = self._commit_exact_handoff_source(context_id, handoff_id)
        completed = self._complete_exact_handoff(
            context_id,
            handoff_id,
            "alice",
            handoff["targetClientId"],
            handoff["targetDeviceSessionId"],
            proof_overrides={"client_seq": 3},
        )
        self.assertTrue(completed.mutated)
        device_state = getDevicePlaybackState(
            context_id,
            handoff["targetClientId"],
        )
        self.assertEqual(device_state["contextEpoch"], 2)
        self.assertEqual(device_state["appliedControlVersion"], 2)
        self.assertEqual(device_state["clientSeq"], 3)
        self.assertEqual(device_state["queueIndex"], 0)

        replay = self._complete_exact_handoff(
            context_id,
            handoff_id,
            "alice",
            handoff["targetClientId"],
            handoff["targetDeviceSessionId"],
            proof_overrides={"client_seq": 3},
        )
        self.assertFalse(replay.mutated)
        self.assertEqual(replay.canonical_context, completed.canonical_context)
        with self.assertRaises(PlaybackClientSequenceConflictError):
            self._complete_exact_handoff(
                context_id,
                handoff_id,
                "alice",
                handoff["targetClientId"],
                handoff["targetDeviceSessionId"],
                position_ms=201,
                proof_overrides={"client_seq": 3},
            )

        ordinary = {
            "playbackContextId": context_id,
            "deviceSessionId": handoff["targetDeviceSessionId"],
            "origin": "passive",
            "appliedControlVersion": 2,
            "state": "playing",
            "trackId": "song-1",
            "positionMs": 200,
            "positionSampledAtServerMs": 1000,
            "playbackRate": 1.0,
            "clientSeq": 3,
        }
        with self.assertRaises(PlaybackClientSequenceConflictError):
            applyStrictPlaybackUpdate(
                context_id,
                "alice",
                handoff["targetClientId"],
                handoff["targetDeviceSessionId"],
                handoff["targetConnectionNonce"],
                ordinary,
                1000,
            )
        ordinary["clientSeq"] = 4
        applied = applyStrictPlaybackUpdate(
            context_id,
            "alice",
            handoff["targetClientId"],
            handoff["targetDeviceSessionId"],
            handoff["targetConnectionNonce"],
            ordinary,
            1000,
        )
        self.assertEqual(applied["canonicalUpdate"]["clientSeq"], 4)

    def test_handoff_complete_exact_replay_ignores_later_resource_fence(self):
        context_id = "handoff-complete-fenced-replay"
        handoff_id = "handoff-complete-fenced-replay"
        handoff = self._commit_exact_handoff_source(context_id, handoff_id)
        completed = self._complete_exact_handoff(
            context_id,
            handoff_id,
            "alice",
            handoff["targetClientId"],
            handoff["targetDeviceSessionId"],
        )
        before_context = getPlaybackContextState(context_id)
        before_handoff = getPlaybackHandoff(handoff_id)
        before_states = getDevicePlaybackStates(context_id)
        db.EmoBroadcastFence.create(
            resource_key="handoff-complete-fenced-replay",
            broadcast_id="broadcast-after-handoff-complete",
            user_name="alice",
            role="ordinary",
            phase="nonterminal",
            playback_context_id=context_id,
            client_id=handoff["targetClientId"],
            device_session_id=handoff["targetDeviceSessionId"],
        )

        replay = self._complete_exact_handoff(
            context_id,
            handoff_id,
            "alice",
            handoff["targetClientId"],
            handoff["targetDeviceSessionId"],
        )

        self.assertFalse(replay.mutated)
        self.assertEqual(replay.canonical_context, completed.canonical_context)
        self.assertEqual(getPlaybackContextState(context_id), before_context)
        self.assertEqual(getPlaybackHandoff(handoff_id), before_handoff)
        self.assertEqual(getDevicePlaybackStates(context_id), before_states)

        with self.assertRaises(PlaybackClientSequenceConflictError):
            self._complete_exact_handoff(
                context_id,
                handoff_id,
                "alice",
                handoff["targetClientId"],
                handoff["targetDeviceSessionId"],
                position_ms=201,
            )
        self.assertEqual(getPlaybackContextState(context_id), before_context)
        self.assertEqual(getPlaybackHandoff(handoff_id), before_handoff)
        self.assertEqual(getDevicePlaybackStates(context_id), before_states)

    def test_handoff_complete_deadline_atomically_times_out_without_switch(self):
        context_id = "handoff-complete-deadline"
        handoff_id = "handoff-complete-deadline"
        handoff = self._commit_exact_handoff_source(context_id, handoff_id)
        before_context = getPlaybackContextState(context_id)
        before_states = getDevicePlaybackStates(context_id)

        result = self._complete_exact_handoff(
            context_id,
            handoff_id,
            "alice",
            handoff["targetClientId"],
            handoff["targetDeviceSessionId"],
            proof_overrides={
                "server_received_at_ms": handoff["completeExpiresAtMs"],
            },
        )

        self.assertFalse(result.mutated)
        self.assertTrue(result.terminalized)
        self.assertEqual(result[1]["status"], "timed_out")
        self.assertEqual(result[1]["errorCode"], "commit_timeout")
        self.assertEqual(getPlaybackContextState(context_id), before_context)
        self.assertEqual(getDevicePlaybackStates(context_id), before_states)
        self.assertEqual(
            getPlaybackContextState(context_id)["authorityClientId"],
            handoff["sourceClientId"],
        )

    def test_handoff_complete_rolls_back_all_writes_on_terminal_save_failure(self):
        context_id = "handoff-complete-rollback"
        handoff_id = "handoff-complete-rollback"
        handoff = self._commit_exact_handoff_source(context_id, handoff_id)
        before_context = getPlaybackContextState(context_id)
        before_handoff = getPlaybackHandoff(handoff_id)
        before_states = getDevicePlaybackStates(context_id)

        with mock.patch.object(
            db.EmoPlaybackHandoff,
            "save",
            side_effect=RuntimeError("injected complete terminal save failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "terminal save failure"):
                self._complete_exact_handoff(
                    context_id,
                    handoff_id,
                    "alice",
                    handoff["targetClientId"],
                    handoff["targetDeviceSessionId"],
                )

        self.assertEqual(getPlaybackContextState(context_id), before_context)
        self.assertEqual(getPlaybackHandoff(handoff_id), before_handoff)
        self.assertEqual(getDevicePlaybackStates(context_id), before_states)

    def test_handoff_commit_source_cursor_change_fails_without_canonical_write(self):
        handoff = self._create_exact_handoff_source(
            handoff_id="handoff-provisional-source-changed",
        )
        record = db.EmoPlaybackContext.get(
            db.EmoPlaybackContext.playback_context_id
            == handoff["playbackContextId"]
        )
        record.version += 1
        record.save(only=(db.EmoPlaybackContext.version,))
        changed_context = getPlaybackContextState(
            handoff["playbackContextId"]
        )

        failed, transitioned = commitStrictPlaybackHandoff(
            handoff["playbackContextId"],
            handoff["handoffId"],
            handoff["userName"],
            9000,
            1200,
            None,
        )

        self.assertTrue(transitioned)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["errorCode"], "source_changed")
        self.assertEqual(
            getPlaybackContextState(handoff["playbackContextId"]),
            changed_context,
        )
        self.assertEqual(
            db.EmoPlaybackControlTransaction.select().count(),
            0,
        )
        with self.assertRaises(PlaybackHandoffTargetConflictError):
            markStrictPlaybackHandoffCommitEnqueued(
                handoff["playbackContextId"],
                handoff["handoffId"],
                handoff["userName"],
            )

    def test_terminal_handoff_releases_provisional_n_plus_one(self):
        handoff = self._create_exact_handoff_source(
            handoff_id="handoff-provisional-release",
        )
        terminated, transitioned = terminateStrictPlaybackHandoff(
            handoff["playbackContextId"],
            handoff["handoffId"],
            handoff["userName"],
            "failed",
            error_code="commit_failed",
        )
        self.assertTrue(transitioned)
        self.assertEqual(terminated["controlVersion"], 2)

        mutated = mutateStrictPlaybackContextControl(
            handoff["playbackContextId"],
            "alice",
            "controller-1",
            "player.pause",
            1,
            requesting_client_id="controller-1",
            requesting_device_session_id="device:controller-1",
            requesting_connection_nonce="requester-nonce-1",
            requesting_connection_epoch=1,
            authority_client_id="source-player",
            authority_device_session_id="device:source-player",
            routed_connection_nonce="source-nonce",
            routed_connection_epoch=1,
            accepted_at_ms=1000,
            execution_timeout_ms=15000,
        )
        self.assertEqual(mutated["controlVersion"], 2)
        self.assertEqual(
            getPlaybackControlTransaction(
                handoff["playbackContextId"],
                1,
                2,
            )["status"],
            "pending",
        )

    def test_structured_handoff_lane_is_immutable(self):
        handoff = self._create_exact_handoff_source(
            handoff_id="handoff-provisional-immutable",
        )
        for field_name, changed_value in (
            ("contextEpoch", 2),
            ("controlVersion", 3),
        ):
            with self.subTest(field=field_name):
                changed = dict(handoff)
                changed[field_name] = changed_value
                with self.assertRaises(PlaybackHandoffTargetConflictError):
                    savePlaybackHandoff(changed)

        changed_snapshot = dict(handoff)
        changed_snapshot["snapshot"] = dict(handoff["snapshot"])
        changed_snapshot["snapshot"]["sourceQueueRevision"] = 2
        with self.assertRaises(PlaybackHandoffTargetConflictError):
            savePlaybackHandoff(changed_snapshot)
        self.assertEqual(
            getPlaybackHandoff(handoff["handoffId"]),
            handoff,
        )

    def _retention_control_values(
        self,
        command_control_version,
        terminal_at_ms,
        playback_context_id="retention-context",
        status="committed",
        depends_on_control_version=None,
        reconciled_by_control_version=None,
        exact_generation=True,
    ):
        created_at_ms = terminal_at_ms or self.RETENTION_NOW_MS
        return {
            "id": uuid4(),
            "playback_context_id": playback_context_id,
            "user_name": "alice",
            "epoch": 1,
            "command_control_version": command_control_version,
            "requesting_client_id": "controller-1",
            "requesting_device_session_id": (
                "device:controller-1" if exact_generation else None
            ),
            "requesting_connection_nonce": (
                "requester-nonce-1" if exact_generation else None
            ),
            "requesting_connection_epoch": 1 if exact_generation else None,
            "authority_client_id": "player-1",
            "authority_device_session_id": "device:player-1",
            "routed_connection_nonce": "authority-nonce-1",
            "routed_connection_epoch": 1,
            "action": "player.seek",
            "accepted_target_json": '{"positionMs":100}',
            "status": status,
            "depends_on_control_version": depends_on_control_version,
            "accepted_at_ms": max(0, created_at_ms - 100),
            "execution_timeout_ms": 15000,
            "terminal_fingerprint": (
                hashlib.sha256(
                    ("%s:%d" % (playback_context_id, command_control_version)).encode(
                        "utf-8"
                    )
                ).hexdigest()
                if terminal_at_ms is not None
                else None
            ),
            "terminal_at_ms": terminal_at_ms,
            "reconciled_by_control_version": reconciled_by_control_version,
            "created_at": datetime.fromtimestamp(created_at_ms / 1000.0),
            "updated_at": datetime.fromtimestamp(created_at_ms / 1000.0),
        }

    def _create_retention_control(self, *args, **kwargs):
        return db.EmoPlaybackControlTransaction.create(
            **self._retention_control_values(*args, **kwargs)
        )

    def _create_retention_intent(
        self,
        control_version,
        created_at_ms,
        playback_context_id="retention-context",
    ):
        return db.EmoPlaybackLocalIntent.create(
            playback_context_id=playback_context_id,
            user_name="alice",
            epoch=1,
            intent_id="intent-%d" % control_version,
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            request_fingerprint=("%064d" % control_version)[-64:],
            canonical_update_json='{"state":"playing"}',
            control_version=control_version,
            superseded_through_control_version=control_version - 1,
            created_at=datetime.fromtimestamp(created_at_ms / 1000.0),
            updated_at=datetime.fromtimestamp(created_at_ms / 1000.0),
        )

    def _create_retention_reconciliation(
        self,
        reconciliation_control_version,
        server_updated_at_ms,
        through_control_version,
        playback_context_id="retention-context",
        trigger_command_control_version=None,
    ):
        return db.EmoPlaybackControlReconciliation.create(
            playback_context_id=playback_context_id,
            user_name="alice",
            epoch=1,
            reconciliation_control_version=reconciliation_control_version,
            from_applied_control_version=0,
            through_control_version=through_control_version,
            trigger_kind="terminal_gap",
            trigger_command_control_version=trigger_command_control_version,
            actual_fact_fingerprint=(
                "%064d" % reconciliation_control_version
            )[-64:],
            actual_fact_json='{"positionMs":0}',
            canonical_update_json='{"positionMs":0}',
            server_updated_at_ms=server_updated_at_ms,
            created_at=datetime.fromtimestamp(server_updated_at_ms / 1000.0),
            updated_at=datetime.fromtimestamp(server_updated_at_ms / 1000.0),
        )

    def _create_feedback_dependency_pair(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2", "song-3"],
            0,
            0,
            "playing",
        )
        applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "authority-nonce-1",
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
            requesting_device_session_id="device:controller-1",
            requesting_connection_nonce="requester-nonce-1",
            requesting_connection_epoch=1,
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="authority-nonce-1",
            routed_connection_epoch=1,
            accepted_at_ms=1100,
            execution_timeout_ms=100,
            deterministic_dependency_admission=True,
        )
        second = mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            first["controlVersion"],
            requesting_client_id="controller-1",
            requesting_device_session_id="device:controller-1",
            requesting_connection_nonce="requester-nonce-1",
            requesting_connection_epoch=1,
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="authority-nonce-1",
            routed_connection_epoch=1,
            accepted_at_ms=1200,
            execution_timeout_ms=100,
            deterministic_dependency_admission=True,
        )
        markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1500,
        )
        return first, second

    def _create_startup_recovery_chain(self):
        transactions = []
        for version, action in (
            (2, "player.next"),
            (3, "queue.playItem"),
            (4, "player.pause"),
        ):
            transaction, created = self._create_exact_transaction(
                command_control_version=version,
                requesting_connection_epoch=1,
                action=action,
                effective_at_server_ms=None,
                execution_timeout_ms=100,
                deterministic_dependency_admission=True,
            )
            self.assertTrue(created)
            transactions.append(transaction)
        markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1500,
        )
        return transactions

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
                    "sourceDeviceSessionId": "device:%s" % source_client_id,
                    "sourceConnectionNonce": "source-nonce-%d" % iteration,
                    "sourceConnectionEpoch": 1,
                    "targetClientId": target_client_id,
                    "targetDeviceSessionId": target_device_session_id,
                    "targetConnectionNonce": "target-nonce-%d" % iteration,
                    "targetConnectionEpoch": 1,
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
            handoff["snapshot"].update(
                {
                    "effectiveAtServerMs": 1000,
                    "positionSampledAtServerMs": 1000,
                    "trackDurationMs": None,
                }
            )
            savePlaybackHandoff(handoff)
            barrier = threading.Barrier(2)

            def complete_handoff():
                barrier.wait()
                try:
                    return self._complete_exact_handoff(
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
                except PlaybackContextHandoffBarrierError:
                    return "handoff_fenced"

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
                self.assertEqual(control_result, "handoff_fenced")
                self.assertEqual(
                    source_context["authorityClientId"],
                    source_client_id,
                )
                self.assertEqual(target_context["lifecycle"], "active")
                self.assertEqual(target_context["state"], "idle")
                self.assertEqual(target_context["controlVersion"], 1)
                self.assertEqual(
                    getPlaybackHandoff(handoff_id)["status"],
                    "committed",
                )
                continue

            self.assertTrue(handoff_result.mutated)
            self.assertIn(
                control_result,
                {"context_closed", "handoff_fenced"},
            )
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

            replay_result = self._complete_exact_handoff(
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
            "sourceDeviceSessionId": "device:source-player",
            "sourceConnectionNonce": "source-nonce",
            "sourceConnectionEpoch": 1,
            "targetClientId": "target-player",
            "targetDeviceSessionId": "device:target-player",
            "targetConnectionNonce": "target-nonce",
            "targetConnectionEpoch": 1,
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
                "sourceDeviceSessionId": "device:source-1",
                "sourceConnectionNonce": "source-nonce-1",
                "sourceConnectionEpoch": 1,
                "targetClientId": "target-1",
                "targetDeviceSessionId": "device:target-1",
                "targetConnectionNonce": "target-nonce-1",
                "targetConnectionEpoch": 1,
                "originClientId": "controller-1",
                "status": "committed",
                "baseControlVersion": 1,
                "contextEpoch": 1,
                "controlVersion": 2,
                "prepareId": "prepare-1",
                "snapshot": {
                    "sourceEpoch": 1,
                    "sourceControlVersion": 1,
                    "handoffControlVersion": 2,
                    "prepareId": "prepare-1",
                    "currentIndex": 0,
                    "trackId": "song-1",
                    "positionMs": 100,
                    "positionSampledAtServerMs": 1000,
                    "playbackRate": 1.0,
                    "effectiveAtServerMs": 1000,
                    "trackDurationMs": None,
                },
            }
        )
        barrier = threading.Barrier(2)

        def complete():
            barrier.wait()
            try:
                result = self._complete_exact_handoff(
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

    def test_complete_handoff_rejects_changed_expected_physical_generation(self):
        createStrictPlaybackContextState(
            "context-generation-cas",
            "alice",
            "source-1",
            "device:source-1",
            ["song-1"],
            0,
            100,
            "playing",
        )
        payload = {
            "handoffId": "handoff-generation-cas",
            "requestId": "handoff-start-generation-cas",
            "playbackContextId": "context-generation-cas",
            "userName": "alice",
            "sourceClientId": "source-1",
            "sourceDeviceSessionId": "device:source-1",
            "sourceConnectionNonce": "source-nonce-cas",
            "sourceConnectionEpoch": 1,
            "targetClientId": "target-1",
            "targetDeviceSessionId": "device:target-1",
            "targetConnectionNonce": "target-nonce-cas",
            "targetConnectionEpoch": 1,
            "originClientId": "controller-1",
            "status": "committed",
            "baseControlVersion": 1,
            "contextEpoch": 1,
            "controlVersion": 2,
            "prepareId": "prepare-generation-cas",
            "snapshot": {
                "sourceEpoch": 1,
                "sourceControlVersion": 1,
                "handoffControlVersion": 2,
                "currentIndex": 0,
                "trackId": "song-1",
                "positionMs": 100,
                "positionSampledAtServerMs": 1000,
                "playbackRate": 1.0,
                "effectiveAtServerMs": 1000,
                "trackDurationMs": None,
            },
        }
        savePlaybackHandoff(payload)
        before_context = getPlaybackContextState("context-generation-cas")
        before_handoff = getPlaybackHandoff("handoff-generation-cas")

        for field_name, value in (
            ("expected_source_connection_nonce", "replacement-source-nonce"),
            ("expected_target_connection_nonce", "replacement-target-nonce"),
        ):
            with self.subTest(field=field_name):
                arguments = {
                    "expected_source_client_id": "source-1",
                    "expected_source_device_session_id": "device:source-1",
                    "expected_source_connection_nonce": "source-nonce-cas",
                    "expected_source_connection_epoch": 1,
                    "expected_target_connection_nonce": "target-nonce-cas",
                    "expected_target_connection_epoch": 1,
                }
                arguments[field_name] = value
                with self.assertRaises(PlaybackHandoffTargetConflictError):
                    completeStrictPlaybackHandoff(
                        "context-generation-cas",
                        "handoff-generation-cas",
                        "alice",
                        "target-1",
                        "device:target-1",
                        queue_index=0,
                        track_id="song-1",
                        state="playing",
                        position_ms=100,
                        position_sampled_at_server_ms=1000,
                        playback_rate=1.0,
                        applied_control_version=2,
                        client_seq=1,
                        server_received_at_ms=1000,
                        **arguments,
                    )
                self.assertEqual(
                    getPlaybackContextState("context-generation-cas"),
                    before_context,
                )
                self.assertEqual(
                    getPlaybackHandoff("handoff-generation-cas"),
                    before_handoff,
                )

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
                "sourceDeviceSessionId": "device:source-1",
                "sourceConnectionNonce": "source-nonce-timeout",
                "sourceConnectionEpoch": 1,
                "targetClientId": "target-1",
                "targetDeviceSessionId": "device:target-1",
                "targetConnectionNonce": "target-nonce-timeout",
                "targetConnectionEpoch": 1,
                "originClientId": "controller-1",
                "status": "committed",
                "baseControlVersion": 1,
                "contextEpoch": 1,
                "controlVersion": 2,
                "prepareId": "prepare-timeout",
                "snapshot": {
                    "sourceEpoch": 1,
                    "sourceControlVersion": 1,
                    "handoffControlVersion": 2,
                    "prepareId": "prepare-timeout",
                    "currentIndex": 0,
                    "trackId": "song-1",
                    "positionMs": 100,
                    "positionSampledAtServerMs": 1000,
                    "playbackRate": 1.0,
                    "effectiveAtServerMs": 1000,
                    "trackDurationMs": None,
                },
            }
        )
        barrier = threading.Barrier(2)

        def complete():
            barrier.wait()
            try:
                result = self._complete_exact_handoff(
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
                "sourceDeviceSessionId": "device:source-1",
                "sourceConnectionNonce": "source-nonce-close",
                "sourceConnectionEpoch": 1,
                "targetClientId": "target-1",
                "targetDeviceSessionId": "device:target-1",
                "targetConnectionNonce": "target-nonce-close",
                "targetConnectionEpoch": 1,
                "originClientId": "controller-1",
                "status": "committed",
                "baseControlVersion": 1,
                "contextEpoch": 1,
                "controlVersion": 2,
                "prepareId": "prepare-close-race",
                "snapshot": {
                    "sourceEpoch": 1,
                    "sourceControlVersion": 1,
                    "handoffControlVersion": 2,
                    "prepareId": "prepare-close-race",
                    "currentIndex": 0,
                    "trackId": "song-1",
                    "positionMs": 100,
                    "positionSampledAtServerMs": 1000,
                    "playbackRate": 1.0,
                    "effectiveAtServerMs": 1000,
                    "trackDurationMs": None,
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
                completed = self._complete_exact_handoff(
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
                "authorityDeviceSessionId": "device:phone-1",
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
                "authorityDeviceSessionId": "device:phone-1",
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

    def test_safe_close_persists_exact_tombstone_and_replays_after_restart(self):
        createStrictPlaybackContextState(
            "safe-close-context",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-1"],
            0,
            250,
            "paused",
        )
        close_kwargs = {
            "expected_epoch": 1,
            "base_version": 1,
            "requesting_client_id": "phone-1",
            "requesting_device_session_id": "device:phone-1",
        }

        closed = closeStrictPlaybackContextState(
            "safe-close-context",
            "alice",
            **close_kwargs,
        )

        self.assertTrue(closed.mutated)
        self.assertEqual(closed.close_outcome, {"action": "playback.context.close"})
        self.assertEqual(closed["lifecycle"], "closed")
        self.assertEqual(closed["version"], 2)
        self.assertEqual(
            closed.affected_authority_pairs,
            (("alice", "phone-1", "device:phone-1"),),
        )
        tombstone = getPlaybackContextCloseTombstone(
            "safe-close-context",
            "alice",
        )
        self.assertEqual(tombstone["closeAction"], "playback.context.close")
        self.assertRegex(tombstone["closeRequestFingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(tombstone["closeExpectedEpoch"], 1)
        self.assertEqual(tombstone["closeBaseVersion"], 1)
        self.assertEqual(tombstone["closedFromEpoch"], 1)
        self.assertEqual(tombstone["closedFromVersion"], 1)
        self.assertEqual(tombstone["finalEpoch"], 1)
        self.assertEqual(tombstone["finalVersion"], 2)
        self.assertEqual(tombstone["finalQueueRevision"], 1)
        self.assertEqual(tombstone["finalControlVersion"], 1)
        self.assertEqual(
            tombstone["closeOutcome"],
            {"action": "playback.context.close"},
        )

        replay = closeStrictPlaybackContextState(
            "safe-close-context",
            "alice",
            **close_kwargs,
        )
        self.assertFalse(replay.mutated)
        self.assertEqual(replay.close_outcome, closed.close_outcome)
        self.assertEqual(replay.tombstone, tombstone)
        self.assertEqual(replay["version"], 2)

        db.release_database()
        db.init_database("sqlite:///" + self.db_path)
        restarted_replay = closeStrictPlaybackContextState(
            "safe-close-context",
            "alice",
            **close_kwargs,
        )
        self.assertFalse(restarted_replay.mutated)
        self.assertEqual(restarted_replay.close_outcome, closed.close_outcome)
        self.assertEqual(restarted_replay.tombstone, tombstone)

    def test_safe_close_mismatch_foreign_and_legacy_rows_fail_closed(self):
        createStrictPlaybackContextState(
            "safe-close-mismatch",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-1"],
            0,
            0,
            "playing",
        )
        closeStrictPlaybackContextState(
            "safe-close-mismatch",
            "alice",
            expected_epoch=1,
            base_version=1,
            requesting_client_id="phone-1",
            requesting_device_session_id="device:phone-1",
        )
        for expected_epoch, base_version in ((2, 1), (1, 2)):
            with self.subTest(
                expected_epoch=expected_epoch,
                base_version=base_version,
            ):
                with self.assertRaises(PlaybackContextClosedError) as mismatch:
                    closeStrictPlaybackContextState(
                        "safe-close-mismatch",
                        "alice",
                        expected_epoch=expected_epoch,
                        base_version=base_version,
                        requesting_client_id="phone-1",
                        requesting_device_session_id="device:phone-1",
                    )
                self.assertEqual(mismatch.exception.playback_context["epoch"], 1)
                self.assertEqual(mismatch.exception.playback_context["version"], 2)
                self.assertEqual(
                    mismatch.exception.playback_context["queueRevision"],
                    1,
                )
                self.assertEqual(
                    mismatch.exception.playback_context["controlVersion"],
                    1,
                )

        self.assertIsNone(
            closeStrictPlaybackContextState(
                "safe-close-mismatch",
                "bob",
                expected_epoch=1,
                base_version=1,
                requesting_client_id="bob-phone",
                requesting_device_session_id="device:bob-phone",
                requester_is_controller=True,
            )
        )
        self.assertIsNone(
            getPlaybackContextStateForUser("safe-close-mismatch", "bob")
        )

        createStrictPlaybackContextState(
            "legacy-close-context",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-1"],
            0,
            0,
            "playing",
        )
        closeStrictPlaybackContextState("legacy-close-context", "alice")
        with self.assertRaises(PlaybackContextClosedError):
            closeStrictPlaybackContextState(
                "legacy-close-context",
                "alice",
                expected_epoch=1,
                base_version=1,
                requesting_client_id="phone-1",
                requesting_device_session_id="device:phone-1",
            )

    def test_safe_close_rejects_fences_stale_cursors_and_partial_tombstone(self):
        def create_context(context_id):
            createStrictPlaybackContextState(
                context_id,
                "alice",
                "phone-1",
                "device:phone-1",
                ["song-1"],
                0,
                0,
                "playing",
            )

        common = {
            "expected_epoch": 1,
            "base_version": 1,
            "requesting_client_id": "controller-1",
            "requesting_device_session_id": "device:controller-1",
            "requester_is_controller": True,
        }
        create_context("pending-close-context")
        createPlaybackControlTransaction(
            "pending-close-context",
            "alice",
            1,
            2,
            "controller-1",
            "phone-1",
            "device:phone-1",
            "nonce-phone",
            1,
            "player.pause",
            {},
            1000,
            5000,
            requesting_device_session_id="device:controller-1",
            requesting_connection_nonce="nonce-controller",
            requesting_connection_epoch=1,
        )
        with self.assertRaises(PlaybackContextCloseConflictError):
            closeStrictPlaybackContextState(
                "pending-close-context",
                "alice",
                **common,
            )
        self.assertEqual(
            getPlaybackContextState("pending-close-context")["lifecycle"],
            "active",
        )

        create_context("handoff-close-context")
        db.EmoPlaybackHandoff.create(
            handoff_id="handoff-close-fence",
            playback_context_id="handoff-close-context",
            user_name="alice",
            source_client_id="phone-1",
            target_client_id="phone-2",
            status="preparing",
        )
        with self.assertRaises(PlaybackContextCloseConflictError):
            closeStrictPlaybackContextState(
                "handoff-close-context",
                "alice",
                **common,
            )
        self.assertEqual(
            getPlaybackHandoff("handoff-close-fence")["status"],
            "preparing",
        )

        create_context("validator-close-context")

        class CloseFenceError(Exception):
            pass

        with self.assertRaises(CloseFenceError):
            closeStrictPlaybackContextState(
                "validator-close-context",
                "alice",
                pre_close_validator=lambda _current: (_ for _ in ()).throw(
                    CloseFenceError("follow fence")
                ),
                **common,
            )
        self.assertEqual(
            getPlaybackContextState("validator-close-context")["version"],
            1,
        )

        create_context("stale-close-context")
        for overrides in (
            {"expected_epoch": 2},
            {"base_version": 2},
        ):
            arguments = dict(common)
            arguments.update(overrides)
            with self.assertRaises(PlaybackContextStaleVersionError):
                closeStrictPlaybackContextState(
                    "stale-close-context",
                    "alice",
                    **arguments,
                )
        self.assertEqual(
            getPlaybackContextState("stale-close-context")["version"],
            1,
        )

        create_context("partial-close-context")
        partial = db.EmoPlaybackContext.get(
            db.EmoPlaybackContext.playback_context_id == "partial-close-context"
        )
        partial.close_action = "playback.context.close"
        partial.save(only=(db.EmoPlaybackContext.close_action,))
        with self.assertRaises(PlaybackContextCloseInvariantError):
            closeStrictPlaybackContextState(
                "partial-close-context",
                "alice",
                **common,
            )
        self.assertEqual(
            getPlaybackContextState("partial-close-context")["lifecycle"],
            "active",
        )

    def test_safe_close_restore_pending_precedes_stale_cursor_without_writes(self):
        createStrictPlaybackContextState(
            "restore-pending-close-context",
            "alice",
            "phone-1",
            "device:phone-1",
            ["song-1"],
            0,
            0,
            "playing",
        )
        db.EmoBroadcastFence.create(
            resource_key="restore-pending-close-fence",
            broadcast_id="broadcast-restore-pending-close",
            user_name="alice",
            role="ordinary",
            phase="restorePending",
            playback_context_id="restore-pending-close-context",
            client_id="phone-1",
            device_session_id="device:phone-1",
        )
        context_before = getPlaybackContextState(
            "restore-pending-close-context"
        )
        validator = mock.Mock()

        with self.assertRaises(PlaybackContextRestoreInProgressError) as blocked:
            closeStrictPlaybackContextState(
                "restore-pending-close-context",
                "alice",
                expected_epoch=9,
                base_version=9,
                requesting_client_id="controller-1",
                requesting_device_session_id="device:controller-1",
                requester_is_controller=True,
                pre_close_validator=validator,
            )

        validator.assert_not_called()
        self.assertEqual(blocked.exception.playback_context, context_before)
        self.assertEqual(
            getPlaybackContextState("restore-pending-close-context"),
            context_before,
        )
        self.assertIsNone(
            getPlaybackContextCloseTombstone(
                "restore-pending-close-context",
                "alice",
            )
        )
        self.assertEqual(
            db.EmoBroadcastFence.select()
            .where(
                db.EmoBroadcastFence.resource_key
                == "restore-pending-close-fence"
            )
            .count(),
            1,
        )

    def test_safe_close_authorizes_exact_authority_pair_or_controller(self):
        for context_id in ("pair-close-context", "controller-close-context"):
            createStrictPlaybackContextState(
                context_id,
                "alice",
                "phone-1",
                "device:phone-1",
                ["song-1"],
                0,
                0,
                "playing",
            )

        with self.assertRaises(PermissionError):
            closeStrictPlaybackContextState(
                "pair-close-context",
                "alice",
                expected_epoch=1,
                base_version=1,
                requesting_client_id="phone-1",
                requesting_device_session_id="device:replacement",
            )
        self.assertEqual(
            getPlaybackContextState("pair-close-context")["lifecycle"],
            "active",
        )

        controller_close = closeStrictPlaybackContextState(
            "controller-close-context",
            "alice",
            expected_epoch=1,
            base_version=1,
            requesting_client_id="controller-1",
            requesting_device_session_id="device:controller-1",
            requester_is_controller=True,
        )
        self.assertTrue(controller_close.mutated)

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

    def test_restart_handoff_recovery_uses_context_pair_database_lock_order(self):
        handoff = self._create_exact_handoff_source(
            playback_context_id="restart-lock-context",
            handoff_id="restart-lock-handoff",
        )
        events = []
        original_context_locks = ws_store._strict_playback_context_lock_set
        original_pair_locks = ws_store._strict_authority_pair_lock
        original_transaction = ws_store._strict_playback_context_transaction

        @contextmanager
        def context_locks(keys):
            events.append(("context", tuple(sorted(keys))))
            with original_context_locks(keys):
                yield

        @contextmanager
        def pair_locks(keys):
            events.append(("pair", tuple(sorted(keys))))
            with original_pair_locks(keys):
                yield

        @contextmanager
        def transaction():
            events.append(("database", None))
            with original_transaction():
                yield

        with mock.patch.object(
            ws_store,
            "_strict_playback_context_lock_set",
            context_locks,
        ), mock.patch.object(
            ws_store,
            "_strict_authority_pair_lock",
            pair_locks,
        ), mock.patch.object(
            ws_store,
            "_strict_playback_context_transaction",
            transaction,
        ):
            reconciled = failActivePlaybackHandoffsForRestart()

        self.assertEqual(reconciled, [handoff["handoffId"]])
        self.assertEqual([event[0] for event in events], ["context", "pair", "database"])
        self.assertEqual(events[0][1], ("restart-lock-context",))
        self.assertEqual(
            events[1][1],
            (
                ("alice", "source-player", "device:source-player"),
                ("alice", "target-player", "device:target-player"),
            ),
        )

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
                "authorityDeviceSessionId": "device:phone-1",
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
        self.assertEqual(
            v2_context["authorityDeviceSessionId"],
            "device:phone-1",
        )

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

    def test_handoff_provisional_lane_is_explicit_and_all_or_none(self):
        legacy = {
            "handoffId": "handoff-legacy-null-lane",
            "requestId": "request-legacy-null-lane",
            "playbackContextId": "playback:alice:legacy-null-lane",
            "userName": "alice",
            "sourceClientId": "phone-1",
            "targetClientId": "pc-1",
            "originClientId": "phone-1",
            "status": "failed",
            "baseControlVersion": 3,
            "controlVersion": 4,
            "snapshot": {
                "sourceEpoch": 1,
                "sourceControlVersion": 3,
                "handoffControlVersion": 4,
            },
        }
        savePlaybackHandoff(legacy)
        legacy_record = db.EmoPlaybackHandoff.get(
            db.EmoPlaybackHandoff.handoff_id == legacy["handoffId"]
        )
        serialized = getPlaybackHandoff(legacy["handoffId"])
        self.assertIsNone(legacy_record.context_epoch)
        self.assertIsNone(legacy_record.provisional_control_version)
        self.assertIsNone(serialized["contextEpoch"])
        self.assertEqual(serialized["controlVersion"], 4)

        structured = dict(legacy)
        structured.update(
            {
                "handoffId": "handoff-structured-invalid",
                "requestId": "request-structured-invalid",
                "status": "preparing",
                "contextEpoch": 1,
            }
        )
        malformed = (
            {key: value for key, value in structured.items() if key != "controlVersion"},
            dict(structured, contextEpoch=True),
            dict(structured, controlVersion=5),
            dict(
                structured,
                snapshot=dict(structured["snapshot"], sourceEpoch=2),
            ),
        )
        for index, payload in enumerate(malformed):
            with self.subTest(case=index):
                payload = dict(payload)
                payload["handoffId"] = "handoff-structured-invalid-%d" % index
                payload["requestId"] = "request-structured-invalid-%d" % index
                with self.assertRaises(PlaybackHandoffTargetConflictError):
                    savePlaybackHandoff(payload)
                self.assertIsNone(getPlaybackHandoff(payload["handoffId"]))

    def test_handoff_snapshot_sanitizer_removes_nested_raw_sid_fields(self):
        savePlaybackHandoff(
            {
                "handoffId": "handoff-sanitized",
                "requestId": "request-sanitized",
                "playbackContextId": "playback:alice:sanitized",
                "userName": "alice",
                "sourceClientId": "phone-1",
                "targetClientId": "pc-1",
                "originClientId": "phone-1",
                "status": "preparing",
                "baseControlVersion": 3,
                "snapshot": {
                    "sid": "root-sid",
                    "businessSidLabel": "preserved",
                    "nested": {
                        "sourceSid": "source-sid",
                        "items": [
                            {"targetSid": "target-sid", "trackId": "song-1"},
                            {"requestSid": "request-sid", "positionMs": 25},
                        ],
                    },
                },
            }
        )

        snapshot = getPlaybackHandoff("handoff-sanitized")["snapshot"]
        self.assertNotIn("sid", snapshot)
        self.assertNotIn("sourceSid", snapshot["nested"])
        self.assertNotIn("targetSid", snapshot["nested"]["items"][0])
        self.assertNotIn("requestSid", snapshot["nested"]["items"][1])
        self.assertEqual(snapshot["businessSidLabel"], "preserved")
        self.assertEqual(snapshot["nested"]["items"][0]["trackId"], "song-1")
        self.assertEqual(snapshot["nested"]["items"][1]["positionMs"], 25)

    def test_existing_handoff_binding_and_generation_are_immutable(self):
        payload = {
            "handoffId": "handoff-immutable-generation",
            "requestId": "request-immutable-generation",
            "playbackContextId": "playback:alice:immutable",
            "userName": "alice",
            "sourceClientId": "source-1",
            "sourceDeviceSessionId": "device:source-1",
            "sourceConnectionNonce": "source-nonce-immutable",
            "sourceConnectionEpoch": 1,
            "targetClientId": "target-1",
            "targetDeviceSessionId": "device:target-1",
            "targetConnectionNonce": "target-nonce-immutable",
            "targetConnectionEpoch": 1,
            "originClientId": "controller-1",
            "status": "preparing",
            "baseControlVersion": 3,
            "controlVersion": 4,
            "snapshot": {"trackId": "song-1"},
        }
        savePlaybackHandoff(payload)
        before = getPlaybackHandoff(payload["handoffId"])

        for field_name, changed_value in (
            ("playbackContextId", "playback:alice:replacement"),
            ("userName", "mallory"),
            ("sourceClientId", "replacement-source"),
            ("sourceDeviceSessionId", "device:replacement-source"),
            ("sourceConnectionNonce", "replacement-source-nonce"),
            ("sourceConnectionEpoch", True),
            ("targetClientId", "replacement-target"),
            ("targetDeviceSessionId", "device:replacement-target"),
            ("targetConnectionNonce", "replacement-target-nonce"),
            ("targetConnectionEpoch", True),
            ("originClientId", "replacement-controller"),
            ("baseControlVersion", 4),
        ):
            with self.subTest(field=field_name):
                changed = dict(payload)
                changed["status"] = "committed"
                changed["snapshot"] = {"trackId": "song-2"}
                changed[field_name] = changed_value
                with self.assertRaises(PlaybackHandoffTargetConflictError):
                    savePlaybackHandoff(changed)
                self.assertEqual(getPlaybackHandoff(payload["handoffId"]), before)

        updated = dict(payload)
        updated["status"] = "committed"
        updated["snapshot"] = {"trackId": "song-2"}
        savePlaybackHandoff(updated)
        persisted = getPlaybackHandoff(payload["handoffId"])
        self.assertEqual(persisted["status"], "committed")
        self.assertEqual(persisted["snapshot"]["trackId"], "song-2")
        for field_name in (
            "playbackContextId",
            "userName",
            "sourceClientId",
            "sourceDeviceSessionId",
            "sourceConnectionNonce",
            "sourceConnectionEpoch",
            "targetClientId",
            "targetDeviceSessionId",
            "targetConnectionNonce",
            "targetConnectionEpoch",
            "originClientId",
            "baseControlVersion",
        ):
            self.assertEqual(persisted[field_name], before[field_name])

    def test_legacy_handoff_terminal_statuses_cannot_reenter_active_lifecycle(self):
        terminal_statuses = (
            "completed",
            "cancelled",
            "failed",
            "timed_out",
            "canceled",
            "timedOut",
            "aborted",
            "superseded",
        )
        for terminal_status in terminal_statuses:
            with self.subTest(status=terminal_status):
                handoff_id = "legacy-terminal-" + terminal_status
                payload = {
                    "handoffId": handoff_id,
                    "requestId": handoff_id + "-request",
                    "playbackContextId": "playback:alice:legacy",
                    "userName": "alice",
                    "sourceClientId": "phone-1",
                    "targetClientId": "pc-1",
                    "originClientId": "phone-1",
                    "status": terminal_status,
                    "baseControlVersion": 3,
                    "snapshot": {"trackId": "song-1"},
                }
                savePlaybackHandoff(payload)
                before = getPlaybackHandoff(handoff_id)

                savePlaybackHandoff(dict(payload))
                self.assertEqual(getPlaybackHandoff(handoff_id), before)

                replay_without_status = dict(payload)
                replay_without_status.pop("status")
                savePlaybackHandoff(replay_without_status)
                self.assertEqual(getPlaybackHandoff(handoff_id), before)

                reactivated = dict(payload)
                reactivated["status"] = "preparing"
                with self.assertRaises(PlaybackHandoffTargetConflictError):
                    savePlaybackHandoff(reactivated)
                self.assertEqual(getPlaybackHandoff(handoff_id), before)

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

        db.release_database()
        db.init_database("sqlite:///" + self.db_path)
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

    def test_terminal_time_cannot_precede_execution_eligibility(self):
        transaction, created = self._create_exact_transaction(
            effective_at_server_ms=None,
            accepted_at_ms=1000,
        )
        self.assertTrue(created)
        eligible, changed = markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1600,
        )
        self.assertTrue(changed)
        self.assertEqual(eligible["executionEligibleAtMs"], 1600)

        with self.assertRaises(PlaybackControlTransactionConflictError):
            settlePlaybackControlTransaction(
                "context-1",
                1,
                2,
                "failed",
                1599,
                error_code="execution_unknown",
            )

        pending = getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["executionEligibleAtMs"], 1600)
        self.assertNotIn("terminalAtMs", pending)

        terminal, changed = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "failed",
            1600,
            error_code="execution_unknown",
        )
        self.assertTrue(changed)
        self.assertEqual(terminal["terminalAtMs"], 1600)

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

    def test_deterministic_dependency_admission_uses_latest_pending_track_change(self):
        transactions = []
        for version, action in (
            (2, "player.pause"),
            (3, "player.seek"),
            (4, "player.next"),
            (5, "player.play"),
            (6, "queue.playItem"),
            (7, "player.pause"),
        ):
            transaction, created = self._create_exact_transaction(
                command_control_version=version,
                action=action,
                effective_at_server_ms=None,
                deterministic_dependency_admission=True,
            )
            self.assertTrue(created)
            transactions.append(transaction)

        self.assertNotIn("dependsOnControlVersion", transactions[0])
        self.assertNotIn("dependsOnControlVersion", transactions[1])
        self.assertNotIn("dependsOnControlVersion", transactions[2])
        self.assertEqual(transactions[3]["dependsOnControlVersion"], 4)
        self.assertEqual(transactions[4]["dependsOnControlVersion"], 4)
        self.assertEqual(transactions[5]["dependsOnControlVersion"], 6)

        with self.assertRaises(ValueError):
            self._create_exact_transaction(
                command_control_version=8,
                deterministic_dependency_admission="yes",
            )
        with self.assertRaises(ValueError):
            self._create_exact_transaction(
                command_control_version=8,
                action="broadcast.play",
                deterministic_dependency_admission=True,
            )
        self.assertEqual(
            db.EmoPlaybackControlTransaction.select().count(),
            6,
        )

        replay, created = self._create_exact_transaction(
            command_control_version=2,
            action="player.pause",
            effective_at_server_ms=None,
            deterministic_dependency_admission=True,
        )
        self.assertFalse(created)
        self.assertNotIn("dependsOnControlVersion", replay)

    def test_dependency_success_establishes_direct_eligibility_atomically(self):
        self._create_exact_transaction(
            command_control_version=2,
            action="player.next",
            effective_at_server_ms=None,
            execution_timeout_ms=100,
            deterministic_dependency_admission=True,
        )
        dependent, _created = self._create_exact_transaction(
            command_control_version=3,
            action="player.next",
            effective_at_server_ms=4000,
            execution_timeout_ms=100,
            deterministic_dependency_admission=True,
        )
        transitive, _created = self._create_exact_transaction(
            command_control_version=4,
            action="player.pause",
            effective_at_server_ms=None,
            execution_timeout_ms=100,
            deterministic_dependency_admission=True,
        )
        self.assertEqual(dependent["dependsOnControlVersion"], 2)
        self.assertEqual(transitive["dependsOnControlVersion"], 3)

        markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1500,
        )
        first_result = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "committed",
            3000,
            applied_control_version=2,
        )

        self.assertTrue(first_result.mutated)
        self.assertEqual(
            [
                item["commandControlVersion"]
                for item in first_result.execution_eligible_transactions
            ],
            [3],
        )
        dependent = getPlaybackControlTransaction("context-1", 1, 3)
        self.assertEqual(dependent["executionEligibleAtMs"], 4000)
        self.assertEqual(dependent["watchdogDeadlineAtMs"], 6100)
        self.assertNotIn(
            "executionEligibleAtMs",
            getPlaybackControlTransaction("context-1", 1, 4),
        )

        second_result = settlePlaybackControlTransaction(
            "context-1",
            1,
            3,
            "committed",
            4500,
            applied_control_version=3,
        )
        self.assertEqual(
            [
                item["commandControlVersion"]
                for item in second_result.execution_eligible_transactions
            ],
            [4],
        )
        transitive = getPlaybackControlTransaction("context-1", 1, 4)
        self.assertEqual(transitive["executionEligibleAtMs"], 4500)
        self.assertEqual(transitive["watchdogDeadlineAtMs"], 6600)

        replay = settlePlaybackControlTransaction(
            "context-1",
            1,
            3,
            "committed",
            4500,
            applied_control_version=3,
        )
        self.assertFalse(replay.mutated)
        self.assertEqual(replay.execution_eligible_transactions, ())
        self.assertEqual(replay.dependency_settlements, ())

    def test_dependency_wait_does_not_consume_execution_timeout(self):
        self._create_exact_transaction(
            command_control_version=2,
            action="player.next",
            effective_at_server_ms=None,
            execution_timeout_ms=10,
            deterministic_dependency_admission=True,
        )
        self._create_exact_transaction(
            command_control_version=3,
            action="player.pause",
            effective_at_server_ms=None,
            execution_timeout_ms=10,
            deterministic_dependency_admission=True,
        )
        markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1000,
        )

        with self.assertRaisesRegex(
            PlaybackControlTransactionConflictError,
            "dependency is not committed",
        ):
            markPlaybackControlTransactionExecutionEligible(
                "context-1",
                1,
                3,
                1000,
            )
        dependent = getPlaybackControlTransaction("context-1", 1, 3)
        self.assertNotIn("executionEligibleAtMs", dependent)
        self.assertNotIn("watchdogDeadlineAtMs", dependent)
        self.assertEqual(
            [
                item["commandControlVersion"]
                for item in listExpiredPlaybackControlTransactions(1000000)
            ],
            [2],
        )

    def test_dependency_failure_cascades_direct_edges_in_version_order(self):
        for version, action in (
            (2, "player.next"),
            (3, "player.next"),
            (4, "player.pause"),
            (5, "player.seek"),
            (6, "queue.playItem"),
            (7, "player.play"),
        ):
            self._create_exact_transaction(
                command_control_version=version,
                action=action,
                effective_at_server_ms=None,
                execution_timeout_ms=100,
                deterministic_dependency_admission=True,
            )
        markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1500,
        )

        result = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "failed",
            3000,
            error_code="track_load_failed",
            applied_control_version=1,
        )

        self.assertEqual(
            [
                item["commandControlVersion"]
                for item in result.dependency_settlements
            ],
            [3, 4, 5, 6, 7],
        )
        expected_direct_dependencies = {
            3: 2,
            4: 3,
            5: 3,
            6: 3,
            7: 6,
        }
        for version, dependency_version in expected_direct_dependencies.items():
            transaction = getPlaybackControlTransaction(
                "context-1",
                1,
                version,
            )
            self.assertEqual(transaction["status"], "failed")
            self.assertEqual(transaction["errorCode"], "dependency_failed")
            self.assertEqual(
                transaction["dependsOnControlVersion"],
                dependency_version,
            )
            self.assertEqual(transaction["terminalAtMs"], 3000)
            self.assertNotIn("executionEligibleAtMs", transaction)
            self.assertNotIn("watchdogDeadlineAtMs", transaction)

        replay = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "failed",
            3000,
            error_code="track_load_failed",
            applied_control_version=1,
        )
        self.assertFalse(replay.mutated)
        self.assertEqual(replay.dependency_settlements, ())
        direct_replay = settlePlaybackControlTransaction(
            "context-1",
            1,
            3,
            "failed",
            3000,
            error_code="dependency_failed",
            depends_on_control_version=2,
            applied_control_version=1,
        )
        self.assertFalse(direct_replay.mutated)

    def test_execution_unknown_cascades_dependency_failed_without_deadlines(self):
        self._create_exact_transaction(
            command_control_version=2,
            action="player.next",
            effective_at_server_ms=None,
            deterministic_dependency_admission=True,
        )
        self._create_exact_transaction(
            command_control_version=3,
            action="player.pause",
            effective_at_server_ms=None,
            deterministic_dependency_admission=True,
        )

        result = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "failed",
            3000,
            error_code="execution_unknown",
            applied_control_version=1,
        )

        self.assertEqual(result.transaction["errorCode"], "execution_unknown")
        self.assertEqual(len(result.dependency_settlements), 1)
        dependent = result.dependency_settlements[0]
        self.assertEqual(dependent["errorCode"], "dependency_failed")
        self.assertEqual(dependent["dependsOnControlVersion"], 2)
        self.assertNotIn("executionEligibleAtMs", dependent)
        self.assertNotIn("watchdogDeadlineAtMs", dependent)

    def test_dependency_supersede_cascades_direct_edges(self):
        for version, action in (
            (2, "player.next"),
            (3, "queue.playItem"),
            (4, "player.pause"),
        ):
            self._create_exact_transaction(
                command_control_version=version,
                action=action,
                effective_at_server_ms=None,
                deterministic_dependency_admission=True,
            )

        result = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "superseded",
            3000,
            applied_control_version=4,
        )

        self.assertEqual(result.transaction["status"], "superseded")
        self.assertEqual(
            [
                item["commandControlVersion"]
                for item in result.dependency_settlements
            ],
            [3, 4],
        )
        for version, dependency_version in ((3, 2), (4, 3)):
            dependent = getPlaybackControlTransaction(
                "context-1",
                1,
                version,
            )
            self.assertEqual(dependent["status"], "failed")
            self.assertEqual(dependent["errorCode"], "dependency_failed")
            self.assertEqual(
                dependent["dependsOnControlVersion"],
                dependency_version,
            )

    def test_dependency_graph_survives_restart_and_resumes_store_transition(self):
        for version, action in (
            (2, "player.next"),
            (3, "player.next"),
            (4, "player.pause"),
        ):
            self._create_exact_transaction(
                command_control_version=version,
                action=action,
                effective_at_server_ms=None,
                execution_timeout_ms=100,
                deterministic_dependency_admission=True,
            )
        markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1500,
        )

        db.release_database()
        db.init_database("sqlite:///" + self.db_path)
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 3)[
                "dependsOnControlVersion"
            ],
            2,
        )
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 4)[
                "dependsOnControlVersion"
            ],
            3,
        )

        result = settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "committed",
            3000,
            applied_control_version=2,
        )
        self.assertEqual(
            [
                item["commandControlVersion"]
                for item in result.execution_eligible_transactions
            ],
            [3],
        )
        self.assertNotIn(
            "executionEligibleAtMs",
            getPlaybackControlTransaction("context-1", 1, 4),
        )

    def test_dependency_transition_rollback_has_no_partial_terminal_or_eligibility(self):
        self._create_exact_transaction(
            command_control_version=2,
            action="player.next",
            effective_at_server_ms=None,
            deterministic_dependency_admission=True,
        )
        self._create_exact_transaction(
            command_control_version=3,
            action="player.pause",
            effective_at_server_ms=None,
            deterministic_dependency_admission=True,
        )
        markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1500,
        )

        with mock.patch(
            "supysonic.emo.ws_store."
            "_mark_control_transaction_record_execution_eligible",
            side_effect=RuntimeError("injected dependency transition failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected dependency transition failure",
            ):
                settlePlaybackControlTransaction(
                    "context-1",
                    1,
                    2,
                    "committed",
                    3000,
                    applied_control_version=2,
                )

        root = getPlaybackControlTransaction("context-1", 1, 2)
        dependent = getPlaybackControlTransaction("context-1", 1, 3)
        self.assertEqual(root["status"], "pending")
        self.assertNotIn("terminalAtMs", root)
        self.assertEqual(dependent["status"], "pending")
        self.assertNotIn("executionEligibleAtMs", dependent)
        self.assertNotIn("watchdogDeadlineAtMs", dependent)

    def test_dependency_cascade_rollback_has_no_partial_terminal(self):
        for version, action in (
            (2, "player.next"),
            (3, "player.next"),
            (4, "player.pause"),
        ):
            self._create_exact_transaction(
                command_control_version=version,
                action=action,
                effective_at_server_ms=None,
                deterministic_dependency_admission=True,
            )

        original_settle = ws_store._settle_playback_control_transaction_record
        cascaded_versions = []

        def fail_during_second_cascade(record, *args, **kwargs):
            if kwargs.get("error_code") == "dependency_failed":
                cascaded_versions.append(record.command_control_version)
                if len(cascaded_versions) == 2:
                    raise RuntimeError("injected cascade failure")
            return original_settle(record, *args, **kwargs)

        with mock.patch(
            "supysonic.emo.ws_store."
            "_settle_playback_control_transaction_record",
            side_effect=fail_during_second_cascade,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected cascade failure"):
                settlePlaybackControlTransaction(
                    "context-1",
                    1,
                    2,
                    "failed",
                    3000,
                    error_code="execution_unknown",
                    applied_control_version=1,
                )

        self.assertEqual(cascaded_versions, [3, 4])
        for version in (2, 3, 4):
            transaction = getPlaybackControlTransaction(
                "context-1",
                1,
                version,
            )
            self.assertEqual(transaction["status"], "pending")
            self.assertNotIn("terminalAtMs", transaction)

    def test_concurrent_dependency_terminal_has_one_durable_winner(self):
        self._create_exact_transaction(
            command_control_version=2,
            action="player.next",
            effective_at_server_ms=None,
            deterministic_dependency_admission=True,
        )
        self._create_exact_transaction(
            command_control_version=3,
            action="player.pause",
            effective_at_server_ms=None,
            deterministic_dependency_admission=True,
        )
        markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            1500,
        )
        start = threading.Barrier(2)

        def settle(status, error_code, applied_control_version):
            start.wait()
            try:
                result = settlePlaybackControlTransaction(
                    "context-1",
                    1,
                    2,
                    status,
                    3000,
                    error_code=error_code,
                    applied_control_version=applied_control_version,
                )
                return "won", result.transaction["status"]
            except PlaybackControlTransactionConflictError:
                return "conflict", None

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    lambda arguments: settle(*arguments),
                    (
                        ("committed", None, 2),
                        ("failed", "execution_unknown", 1),
                    ),
                )
            )

        self.assertEqual([outcome[0] for outcome in outcomes].count("won"), 1)
        self.assertEqual(
            [outcome[0] for outcome in outcomes].count("conflict"),
            1,
        )
        root = getPlaybackControlTransaction("context-1", 1, 2)
        dependent = getPlaybackControlTransaction("context-1", 1, 3)
        if root["status"] == "committed":
            self.assertEqual(dependent["status"], "pending")
            self.assertEqual(dependent["executionEligibleAtMs"], 3000)
        else:
            self.assertEqual(root["errorCode"], "execution_unknown")
            self.assertEqual(dependent["status"], "failed")
            self.assertEqual(dependent["errorCode"], "dependency_failed")

    def test_store_feedback_commit_releases_only_direct_dependency(self):
        _first, second = self._create_feedback_dependency_pair()
        self.assertEqual(
            second["_controlTransaction"]["dependsOnControlVersion"],
            2,
        )

        result = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "authority-nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "remoteCommand",
                "executionStatus": "committed",
                "commandControlVersion": 2,
                "appliedControlVersion": 2,
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 0,
                "clientSeq": 2,
            },
            3000,
            require_execution_eligible=True,
        )

        self.assertEqual(result["dependencySettlements"], [])
        self.assertEqual(
            [
                item["commandControlVersion"]
                for item in result["executionEligibleTransactions"]
            ],
            [3],
        )
        dependent = getPlaybackControlTransaction("context-1", 1, 3)
        self.assertEqual(dependent["status"], "pending")
        self.assertEqual(dependent["executionEligibleAtMs"], 3000)
        self.assertEqual(dependent["watchdogDeadlineAtMs"], 5100)

    def test_feedback_and_server_unknown_have_one_dependency_outcome(self):
        self._create_feedback_dependency_pair()
        start = threading.Barrier(2)

        def apply_feedback():
            start.wait()
            try:
                applyStrictPlaybackUpdate(
                    "context-1",
                    "alice",
                    "player-1",
                    "device:player-1",
                    "authority-nonce-1",
                    {
                        "playbackContextId": "context-1",
                        "deviceSessionId": "device:player-1",
                        "origin": "remoteCommand",
                        "executionStatus": "committed",
                        "commandControlVersion": 2,
                        "appliedControlVersion": 2,
                        "state": "playing",
                        "trackId": "song-2",
                        "positionMs": 0,
                        "clientSeq": 2,
                    },
                    3000,
                    require_execution_eligible=True,
                )
                return "feedback"
            except PlaybackControlTransactionConflictError:
                return "conflict"

        def settle_unknown():
            start.wait()
            try:
                settlePlaybackControlTransaction(
                    "context-1",
                    1,
                    2,
                    "failed",
                    3000,
                    error_code="execution_unknown",
                    applied_control_version=1,
                )
                return "unknown"
            except PlaybackControlTransactionConflictError:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [
                executor.submit(apply_feedback),
                executor.submit(settle_unknown),
            ]
            outcomes = [future.result() for future in outcomes]

        self.assertEqual(outcomes.count("conflict"), 1)
        root = getPlaybackControlTransaction("context-1", 1, 2)
        dependent = getPlaybackControlTransaction("context-1", 1, 3)
        device_state = getDevicePlaybackState("context-1", "player-1")
        if root["status"] == "committed":
            self.assertIn("feedback", outcomes)
            self.assertEqual(dependent["status"], "pending")
            self.assertEqual(dependent["executionEligibleAtMs"], 3000)
            self.assertEqual(device_state["clientSeq"], 2)
            self.assertEqual(device_state["appliedControlVersion"], 2)
        else:
            self.assertIn("unknown", outcomes)
            self.assertEqual(root["errorCode"], "execution_unknown")
            self.assertEqual(dependent["status"], "failed")
            self.assertEqual(dependent["errorCode"], "dependency_failed")
            self.assertEqual(device_state["clientSeq"], 1)
            self.assertEqual(device_state["appliedControlVersion"], 1)

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

    def test_legacy_dependency_terminal_fingerprint_replays_unchanged(self):
        self._create_exact_transaction(
            command_control_version=2,
            action="player.next",
            effective_at_server_ms=None,
            deterministic_dependency_admission=True,
        )
        self._create_exact_transaction(
            command_control_version=3,
            action="player.pause",
            effective_at_server_ms=None,
            deterministic_dependency_admission=True,
        )
        record = db.EmoPlaybackControlTransaction.get(
            (db.EmoPlaybackControlTransaction.playback_context_id == "context-1")
            & (db.EmoPlaybackControlTransaction.epoch == 1)
            & (db.EmoPlaybackControlTransaction.command_control_version == 3)
        )
        old_terminal = {
            "status": "failed",
            "errorCode": "dependency_failed",
            "dependsOnControlVersion": 2,
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
        record.error_code = "dependency_failed"
        record.applied_control_version = 1
        record.terminal_fingerprint = old_fingerprint
        record.terminal_at_ms = 2000
        record.save()

        replay = settlePlaybackControlTransaction(
            "context-1",
            1,
            3,
            "failed",
            2000,
            error_code="dependency_failed",
            applied_control_version=1,
        )

        self.assertFalse(replay.mutated)
        self.assertEqual(replay.transaction["dependsOnControlVersion"], 2)
        self.assertEqual(replay.transaction["terminalFingerprint"], old_fingerprint)
        persisted = db.EmoPlaybackControlTransaction.get_by_id(record.id)
        self.assertEqual(persisted.terminal_fingerprint, old_fingerprint)
        self.assertIsNone(persisted.error_message)

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

    def test_ordinary_remote_feedback_requires_execution_eligibility(self):
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
        mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.pause",
            1,
            requesting_client_id="controller-1",
            requesting_device_session_id="device:controller-1",
            requesting_connection_nonce="requester-nonce-1",
            requesting_connection_epoch=3,
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="authority-nonce-1",
            routed_connection_epoch=1,
            accepted_at_ms=1000,
            execution_timeout_ms=15000,
            effective_at_server_ms=1500,
        )
        before_context = getPlaybackContextState("context-1")
        payload = {
            "playbackContextId": "context-1",
            "deviceSessionId": "device:player-1",
            "origin": "remoteCommand",
            "executionStatus": "committed",
            "commandControlVersion": 2,
            "appliedControlVersion": 2,
            "state": "paused",
            "trackId": "song-1",
            "positionMs": 10,
            "clientSeq": 1,
        }

        with self.assertRaisesRegex(
            PlaybackControlTransactionConflictError,
            "not execution eligible",
        ):
            applyStrictPlaybackUpdate(
                "context-1",
                "alice",
                "player-1",
                "device:player-1",
                "authority-nonce-1",
                payload,
                2000,
                require_execution_eligible=True,
            )

        self.assertEqual(getPlaybackContextState("context-1"), before_context)
        self.assertIsNone(getDevicePlaybackState("context-1", "player-1"))
        self.assertEqual(
            db.EmoPlaybackControlTransaction.select().count(),
            1,
        )
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2)["status"],
            "pending",
        )

        eligible, changed = markPlaybackControlTransactionExecutionEligible(
            "context-1",
            1,
            2,
            2000,
        )
        self.assertTrue(changed)
        self.assertIsNotNone(eligible["executionEligibleAtMs"])
        self.assertIsNotNone(eligible["watchdogDeadlineAtMs"])

        committed = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "authority-nonce-1",
            payload,
            2100,
            require_execution_eligible=True,
        )
        self.assertEqual(committed["canonicalUpdate"]["controlVersion"], 2)
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2)["status"],
            "committed",
        )

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
        self.assertEqual(context["controlVersion"], 4)
        self.assertEqual(context["currentIndex"], 0)
        self.assertEqual(context["trackId"], "song-1")

    def test_failed_feedback_reconciles_terminal_gap_inline_once(self):
        self._create_feedback_dependency_pair()
        previous_context = getPlaybackContextState("context-1")
        observed_previous = []
        payload = {
            "playbackContextId": "context-1",
            "deviceSessionId": "device:player-1",
            "origin": "remoteCommand",
            "executionStatus": "failed",
            "commandControlVersion": 2,
            "appliedControlVersion": 1,
            "errorCode": "track_load_failed",
            "errorMessage": "decoder rejected the track",
            "state": "playing",
            "trackId": "song-1",
            "positionMs": 25,
            "positionSampledAtServerMs": 1590,
            "playbackRate": 1.25,
            "clientSeq": 2,
        }

        result = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "authority-nonce-1",
            payload,
            1600,
            post_mutation_hook=lambda _record, _result, previous, _device: (
                observed_previous.append(previous)
            ),
            require_execution_eligible=True,
        )

        self.assertEqual(observed_previous, [previous_context])
        self.assertEqual(result["canonicalUpdate"]["controlVersion"], 4)
        self.assertEqual(result["canonicalUpdate"]["appliedControlVersion"], 4)
        self.assertEqual(result["canonicalUpdate"]["commandControlVersion"], 2)
        self.assertEqual(result["canonicalUpdate"]["executionStatus"], "failed")
        self.assertEqual(result["canonicalUpdate"]["errorCode"], "track_load_failed")
        self.assertEqual(
            result["canonicalUpdate"]["errorMessage"],
            "decoder rejected the track",
        )
        reconciliation = result["controlReconciliation"]
        self.assertEqual(reconciliation["reconciliationControlVersion"], 4)
        self.assertEqual(reconciliation["fromAppliedControlVersion"], 1)
        self.assertEqual(reconciliation["throughControlVersion"], 3)
        self.assertEqual(reconciliation["triggerKind"], "remote_failed")
        self.assertEqual(reconciliation["triggerCommandControlVersion"], 2)

        context = getPlaybackContextState("context-1")
        device = getDevicePlaybackState("context-1", "player-1")
        self.assertEqual(context["controlVersion"], 4)
        self.assertEqual(context["version"], previous_context["version"] + 1)
        self.assertEqual(context["queueRevision"], 3)
        self.assertEqual(context["currentIndex"], 0)
        self.assertEqual(context["trackId"], "song-1")
        self.assertEqual(context["state"], "playing")
        self.assertEqual(context["positionMs"], 25)
        self.assertEqual(device["appliedControlVersion"], 4)
        self.assertEqual(device["playbackRate"], 1.25)
        for command_version, error_code in (
            (2, "track_load_failed"),
            (3, "dependency_failed"),
        ):
            transaction = getPlaybackControlTransaction(
                "context-1",
                1,
                command_version,
            )
            self.assertEqual(transaction["status"], "failed")
            self.assertEqual(transaction["errorCode"], error_code)
            self.assertEqual(transaction["reconciledByControlVersion"], 4)
            self.assertIsNotNone(transaction["terminalFingerprint"])
        self.assertEqual(db.EmoPlaybackControlTransaction.select().count(), 2)
        self.assertEqual(db.EmoPlaybackControlReconciliation.select().count(), 1)

        duplicate = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "authority-nonce-1",
            payload,
            1700,
            require_execution_eligible=True,
        )
        self.assertTrue(duplicate["sourceOnly"])
        self.assertEqual(duplicate["canonicalUpdate"], result["canonicalUpdate"])
        self.assertEqual(getPlaybackContextState("context-1"), context)
        self.assertEqual(db.EmoPlaybackControlReconciliation.select().count(), 1)

    def test_passive_fact_reconciles_server_unknown_gap(self):
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
            "authority-nonce-1",
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
            requesting_client_id="controller-1",
            requesting_device_session_id="device:controller-1",
            requesting_connection_nonce="requester-nonce-1",
            requesting_connection_epoch=1,
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="authority-nonce-1",
            routed_connection_epoch=1,
            accepted_at_ms=1100,
            execution_timeout_ms=100,
        )
        markPlaybackControlTransactionExecutionEligible("context-1", 1, 2, 1200)
        settlePlaybackControlTransaction(
            "context-1",
            1,
            2,
            "failed",
            1300,
            error_code="execution_unknown",
            applied_control_version=1,
        )
        before = getPlaybackContextState("context-1")

        result = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "authority-nonce-1",
            {
                "playbackContextId": "context-1",
                "deviceSessionId": "device:player-1",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 175,
                "positionSampledAtServerMs": 1390,
                "playbackRate": 1.5,
                "clientSeq": 2,
            },
            1400,
        )

        self.assertEqual(result["canonicalUpdate"]["origin"], "passive")
        self.assertEqual(result["canonicalUpdate"]["controlVersion"], 3)
        self.assertEqual(result["canonicalUpdate"]["appliedControlVersion"], 3)
        self.assertEqual(
            result["controlReconciliation"]["triggerKind"],
            "passive_terminal_gap",
        )
        self.assertEqual(
            result["controlReconciliation"]["canonicalUpdate"],
            result["canonicalUpdate"],
        )
        after = getPlaybackContextState("context-1")
        self.assertEqual(after["version"], before["version"] + 1)
        self.assertEqual(after["controlVersion"], 3)
        self.assertEqual(after["queueRevision"], before["queueRevision"])
        self.assertEqual(after["state"], "playing")
        self.assertEqual(after["positionMs"], 175)
        self.assertEqual(
            getDevicePlaybackState("context-1", "player-1")[
                "appliedControlVersion"
            ],
            3,
        )
        terminal = getPlaybackControlTransaction("context-1", 1, 2)
        self.assertEqual(terminal["errorCode"], "execution_unknown")
        self.assertEqual(terminal["reconciledByControlVersion"], 3)

    def test_failed_feedback_waits_for_all_pending_before_passive_reconciliation(self):
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
                "clientSeq": 1,
            },
            1000,
        )
        for action, base_version, accepted_at_ms in (
            ("player.pause", 1, 1100),
            ("player.play", 2, 1200),
        ):
            mutateStrictPlaybackContextControl(
                "context-1",
                "alice",
                "controller-1",
                action,
                base_version,
                requesting_client_id="controller-1",
                requesting_device_session_id="device:controller-1",
                requesting_connection_nonce="requester-nonce-1",
                requesting_connection_epoch=1,
                authority_client_id="player-1",
                authority_device_session_id="device:player-1",
                routed_connection_nonce="nonce-1",
                routed_connection_epoch=1,
                accepted_at_ms=accepted_at_ms,
                execution_timeout_ms=100,
                deterministic_dependency_admission=True,
            )
        markPlaybackControlTransactionExecutionEligible("context-1", 1, 2, 1300)
        markPlaybackControlTransactionExecutionEligible("context-1", 1, 3, 1300)

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
                "errorCode": "playback_failed",
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 100,
                "clientSeq": 2,
            },
            1400,
            require_execution_eligible=True,
        )
        self.assertNotIn("controlReconciliation", failed)
        self.assertEqual(failed["canonicalUpdate"]["controlVersion"], 3)
        self.assertEqual(failed["canonicalUpdate"]["appliedControlVersion"], 1)
        self.assertEqual(db.EmoPlaybackControlReconciliation.select().count(), 0)
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 3)["status"],
            "pending",
        )

        settlePlaybackControlTransaction(
            "context-1",
            1,
            3,
            "failed",
            1500,
            error_code="execution_unknown",
            applied_control_version=1,
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
                "positionMs": 125,
                "clientSeq": 3,
            },
            1600,
        )
        self.assertEqual(passive["canonicalUpdate"]["controlVersion"], 4)
        self.assertEqual(passive["canonicalUpdate"]["appliedControlVersion"], 4)
        self.assertEqual(db.EmoPlaybackControlReconciliation.select().count(), 1)
        for command_version in (2, 3):
            self.assertEqual(
                getPlaybackControlTransaction(
                    "context-1",
                    1,
                    command_version,
                )["reconciledByControlVersion"],
                4,
            )

    def test_reconciliation_rolls_back_with_post_mutation_failure(self):
        self._create_feedback_dependency_pair()
        before_context = getPlaybackContextState("context-1")
        before_device = getDevicePlaybackState("context-1", "player-1")

        with self.assertRaisesRegex(RuntimeError, "projection failed"):
            applyStrictPlaybackUpdate(
                "context-1",
                "alice",
                "player-1",
                "device:player-1",
                "authority-nonce-1",
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
                1600,
                post_mutation_hook=lambda *_args: (_ for _ in ()).throw(
                    RuntimeError("projection failed")
                ),
                require_execution_eligible=True,
            )

        self.assertEqual(getPlaybackContextState("context-1"), before_context)
        self.assertEqual(
            getDevicePlaybackState("context-1", "player-1"),
            before_device,
        )
        self.assertEqual(db.EmoPlaybackControlReconciliation.select().count(), 0)
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2)["status"],
            "pending",
        )
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 3)["status"],
            "pending",
        )

    def test_first_prev_and_last_next_preserve_queue_revision(self):
        cases = (
            ("player.prev", 0, "paused", "playing", "song-1"),
            ("player.next", 1, "playing", "stopped", "song-2"),
        )
        for action, index, initial_state, expected_state, track_id in cases:
            with self.subTest(action=action):
                context_id = "context-%s" % action
                createStrictPlaybackContextState(
                    context_id,
                    "alice",
                    "player-1",
                    "device:player-1",
                    ["song-1", "song-2"],
                    index,
                    500,
                    initial_state,
                )
                before = getPlaybackContextState(context_id)
                updated = mutateStrictPlaybackContextControl(
                    context_id,
                    "alice",
                    "controller-1",
                    action,
                    1,
                    position_ms=0,
                )

                self.assertEqual(updated["version"], before["version"] + 1)
                self.assertEqual(
                    updated["controlVersion"],
                    before["controlVersion"] + 1,
                )
                self.assertEqual(
                    updated["queueRevision"],
                    before["queueRevision"],
                )
                self.assertEqual(updated["currentIndex"], index)
                self.assertEqual(updated["trackId"], track_id)
                self.assertEqual(updated["state"], expected_state)
                self.assertEqual(updated["positionMs"], 0)
                deletePlaybackContext(context_id)

    def test_single_item_natural_terminal_advances_context_version_once(self):
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
                "clientSeq": 1,
            },
            1000,
        )
        before = getPlaybackContextState("context-1")
        terminal_payload = {
            "playbackContextId": "context-1",
            "deviceSessionId": "device:player-1",
            "origin": "passive",
            "appliedControlVersion": 1,
            "state": "stopped",
            "trackId": "song-1",
            "positionMs": 0,
            "positionSampledAtServerMs": 1090,
            "playbackRate": 1.0,
            "clientSeq": 2,
        }

        terminal = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            terminal_payload,
            1100,
        )
        after = getPlaybackContextState("context-1")
        self.assertTrue(terminal["naturalTerminal"])
        self.assertEqual(after["state"], "stopped")
        self.assertEqual(after["positionMs"], 0)
        self.assertEqual(after["version"], before["version"] + 1)
        self.assertEqual(after["epoch"], before["epoch"])
        self.assertEqual(after["queueRevision"], before["queueRevision"])
        self.assertEqual(after["controlVersion"], before["controlVersion"])

        duplicate = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            terminal_payload,
            1200,
        )
        self.assertTrue(duplicate["sourceOnly"])
        self.assertEqual(getPlaybackContextState("context-1"), after)

        fresh_repeat_payload = dict(terminal_payload)
        fresh_repeat_payload["clientSeq"] = 3
        fresh_repeat = applyStrictPlaybackUpdate(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            "nonce-1",
            fresh_repeat_payload,
            1300,
        )
        self.assertNotIn("naturalTerminal", fresh_repeat)
        self.assertEqual(getPlaybackContextState("context-1"), after)
        self.assertEqual(
            getDevicePlaybackState("context-1", "player-1")["clientSeq"],
            3,
        )

    def test_natural_terminal_does_not_advance_context_with_pending_control(self):
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
                "clientSeq": 1,
            },
            1000,
        )
        mutateStrictPlaybackContextControl(
            "context-1",
            "alice",
            "controller-1",
            "player.seek",
            1,
            position_ms=200,
            requesting_client_id="controller-1",
            authority_client_id="player-1",
            authority_device_session_id="device:player-1",
            routed_connection_nonce="nonce-1",
            accepted_at_ms=1100,
            execution_timeout_ms=100,
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
                "state": "stopped",
                "trackId": "song-1",
                "positionMs": 0,
                "clientSeq": 2,
            },
            1200,
        )

        self.assertNotIn("naturalTerminal", result)
        self.assertNotIn("controlReconciliation", result)
        self.assertEqual(getPlaybackContextState("context-1"), before)
        self.assertEqual(
            getDevicePlaybackState("context-1", "player-1")["state"],
            "stopped",
        )
        self.assertEqual(
            getPlaybackControlTransaction("context-1", 1, 2)["status"],
            "pending",
        )

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

    def test_handoff_source_passive_progress_uses_current_device_baseline(self):
        handoff = self._create_exact_handoff_source()

        first_progress = applyStrictPlaybackUpdate(
            "handoff-source-context",
            "alice",
            "source-player",
            "device:source-player",
            handoff["sourceConnectionNonce"],
            {
                "playbackContextId": "handoff-source-context",
                "deviceSessionId": "device:source-player",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 250,
                "positionSampledAtServerMs": 1100,
                "playbackRate": 1.0,
                "clientSeq": 2,
            },
            1200,
        )
        second_progress = applyStrictPlaybackUpdate(
            "handoff-source-context",
            "alice",
            "source-player",
            "device:source-player",
            handoff["sourceConnectionNonce"],
            {
                "playbackContextId": "handoff-source-context",
                "deviceSessionId": "device:source-player",
                "origin": "passive",
                "appliedControlVersion": 1,
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 300,
                "positionSampledAtServerMs": 1200,
                "playbackRate": 1.0,
                "clientSeq": 3,
            },
            1300,
        )

        self.assertNotIn("handoffSettlement", first_progress)
        self.assertNotIn("handoffSettlement", second_progress)
        self.assertEqual(getPlaybackHandoff("handoff-source-1")["status"], "preparing")
        self.assertEqual(
            getDevicePlaybackState("handoff-source-context", "source-player")[
                "positionMs"
            ],
            300,
        )

    def test_handoff_source_changed_precedes_actual_and_rolls_back_atomically(self):
        handoff = self._create_exact_handoff_source()
        events = []
        original_settle = ws_store._settle_handoff_source_changed
        original_save_device = ws_store._save_strict_device_state_record

        def settle(record):
            events.append("handoff_terminal")
            return original_settle(record)

        def save_device(*args, **kwargs):
            events.append("device_actual")
            return original_save_device(*args, **kwargs)

        changed_payload = {
            "playbackContextId": "handoff-source-context",
            "deviceSessionId": "device:source-player",
            "origin": "passive",
            "appliedControlVersion": 1,
            "state": "paused",
            "trackId": "song-1",
            "positionMs": 250,
            "positionSampledAtServerMs": 1100,
            "playbackRate": 1.0,
            "clientSeq": 2,
        }
        with mock.patch.object(
            ws_store,
            "_settle_handoff_source_changed",
            side_effect=settle,
        ), mock.patch.object(
            ws_store,
            "_save_strict_device_state_record",
            side_effect=save_device,
        ):
            changed = applyStrictPlaybackUpdate(
                "handoff-source-context",
                "alice",
                "source-player",
                "device:source-player",
                handoff["sourceConnectionNonce"],
                changed_payload,
                1200,
            )

        self.assertEqual(events, ["handoff_terminal", "device_actual"])
        self.assertEqual(changed["handoffSettlement"]["status"], "failed")
        self.assertEqual(
            changed["handoffSettlement"]["errorCode"],
            "source_changed",
        )
        self.assertEqual(getPlaybackHandoff("handoff-source-1")["status"], "failed")
        self.assertEqual(
            getDevicePlaybackState("handoff-source-context", "source-player")["state"],
            "paused",
        )

        rollback_handoff = self._create_exact_handoff_source(
            playback_context_id="handoff-rollback-context",
            handoff_id="handoff-rollback",
        )
        before_device = getDevicePlaybackState(
            "handoff-rollback-context",
            "source-player",
        )
        rollback_payload = dict(changed_payload)
        rollback_payload["playbackContextId"] = "handoff-rollback-context"

        def fail_after_mutation(*_args):
            raise RuntimeError("injected source update rollback")

        with self.assertRaisesRegex(RuntimeError, "injected source update rollback"):
            applyStrictPlaybackUpdate(
                "handoff-rollback-context",
                "alice",
                "source-player",
                "device:source-player",
                rollback_handoff["sourceConnectionNonce"],
                rollback_payload,
                1200,
                post_mutation_hook=fail_after_mutation,
            )

        self.assertEqual(getPlaybackHandoff("handoff-rollback")["status"], "preparing")
        self.assertEqual(
            getDevicePlaybackState("handoff-rollback-context", "source-player"),
            before_device,
        )

    def test_stale_passive_applied_version_raises_conflict_without_mutation(self):
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
        before_context = getPlaybackContextState("context-1")
        before_device = getDevicePlaybackState("context-1", "player-1")
        with self.assertRaises(PlaybackPassiveAppliedVersionConflictError):
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
                    "clientSeq": 3,
                },
                1100,
            )
        self.assertEqual(getPlaybackContextState("context-1"), before_context)
        self.assertEqual(
            getDevicePlaybackState("context-1", "player-1"),
            before_device,
        )

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

    def test_ensure_ignores_legacy_null_rows_but_fails_on_real_candidates(self):
        createStrictPlaybackContextState(
            "legacy-null-context",
            "alice",
            "player-1",
            None,
            ["legacy-song"],
            0,
            0,
            "paused",
        )
        for context_id, device_session_id in (
            ("context-old-1", "device:old-1"),
            ("context-old-2", "device:old-2"),
        ):
            createStrictPlaybackContextState(
                context_id,
                "alice",
                "player-1",
                device_session_id,
                ["song-1"],
                0,
                0,
                "paused",
            )

        with self.assertRaises(PlaybackContextEnsureConflictError):
            ensureStrictPlaybackContextState(
                "alice",
                "player-1",
                "device:current",
                [],
                None,
                0,
                "idle",
            )

        self.assertEqual(
            listActivePlaybackContextBindings(
                "alice",
                "player-1",
                "device:current",
            ),
            [],
        )
        contexts = listUserPlaybackContexts("alice")
        self.assertEqual(
            {context["playbackContextId"] for context in contexts},
            {
                "legacy-null-context",
                "context-old-1",
                "context-old-2",
            },
        )
        self.assertTrue(
            all(context["lifecycle"] == "active" for context in contexts)
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

    def test_ensure_recreates_context_when_only_legacy_active_rows_remain(self):
        legacy_context_ids = (
            "device:player-1",
            "player-1",
        )
        for context_id in legacy_context_ids:
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
            "playback:closed-canonical",
            "alice",
            "player-1",
            "device:player-1",
            ["old-song"],
            0,
            250,
            "playing",
        )
        closeStrictPlaybackContextState(
            "playback:closed-canonical",
            "alice",
        )

        result = ensureStrictPlaybackContextState(
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            500,
            "playing",
        )

        recreated, mutated = result
        self.assertTrue(mutated)
        self.assertTrue(result.binding_mutated)
        self.assertTrue(recreated["playbackContextId"].startswith("playback:"))
        self.assertNotIn(recreated["playbackContextId"], legacy_context_ids)
        self.assertNotEqual(
            recreated["playbackContextId"],
            "playback:closed-canonical",
        )
        self.assertEqual(
            recreated["authorityDeviceSessionId"],
            "device:player-1",
        )
        self.assertEqual(recreated["queueSongIds"], ["song-1"])
        self.assertEqual(
            listActivePlaybackContextBindings(
                "alice",
                "player-1",
                "device:player-1",
            ),
            [
                {
                    "playbackContextId": recreated["playbackContextId"],
                    "authorityClientId": "player-1",
                    "authorityDeviceSessionId": "device:player-1",
                }
            ],
        )

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
        baseline_before = getDevicePlaybackState("context-1", "player-1")
        context_before = getPlaybackContextState("context-1")
        for position_ms in (0, 1):
            conflicting_stale = dict(stale_payload)
            conflicting_stale["positionMs"] = position_ms
            with self.assertRaises(PlaybackPassiveAppliedVersionConflictError):
                applyStrictPlaybackUpdate(
                    "context-1",
                    "alice",
                    "player-1",
                    "device:player-1",
                    "nonce-1",
                    conflicting_stale,
                    1300,
                )
            self.assertEqual(
                getPlaybackContextState("context-1"),
                context_before,
            )
            self.assertEqual(
                getDevicePlaybackState("context-1", "player-1"),
                baseline_before,
            )
            self.assertEqual(getDevicePlaybackStates("context-1"), [])

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
                "clientSeq": 1,
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
            1,
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
        self.assertEqual(reconciled["controlVersion"], 3)
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
        self.assertEqual(advanced["controlVersion"], 4)
        self.assertEqual(
            getDevicePlaybackState("context-1", "player-1")[
                "appliedControlVersion"
            ],
            4,
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

    def test_restore_pending_prepare_cleanup_is_narrow_and_idempotent(self):
        context = createStrictPlaybackContextState(
            "restore-prepare-context",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1"],
            0,
            0,
            "paused",
        ).canonical_context
        createPlaybackPrepareTransaction(
            "restore-prepare-context",
            "alice",
            context["epoch"],
            "restore-prepare-intent",
            "controller-1",
            "player-1",
            "device:player-1",
            "player-nonce-1",
            1,
            {},
            context["controlVersion"],
            11000,
        )
        db.EmoBroadcastFence.create(
            resource_key="restore-prepare-fence",
            broadcast_id="restore-prepare-broadcast",
            user_name="alice",
            role="ordinary",
            phase="restorePending",
            playback_context_id="restore-prepare-context",
            client_id="player-1",
            device_session_id="device:player-1",
        )
        context_before = getPlaybackContextState("restore-prepare-context")
        negative = {
            "playbackContextId": "restore-prepare-context",
            "intentId": "restore-prepare-intent",
            "ready": False,
            "errorCode": "restore_in_progress",
            "controlVersion": context["controlVersion"],
        }

        with self.assertRaises(PlaybackContextRestoreInProgressError):
            settlePlaybackPrepareTransaction(
                "restore-prepare-context",
                context["epoch"],
                "restore-prepare-intent",
                "ready",
                dict(negative, ready=True),
                1000,
            )
        with self.assertRaisesRegex(
            ValueError,
            "matching negative result",
        ):
            settlePlaybackPrepareTransaction(
                "restore-prepare-context",
                context["epoch"],
                "restore-prepare-intent",
                "failed",
                dict(negative, errorCode="queue_required"),
                1000,
                error_code="queue_required",
                allow_restore_pending_cleanup=True,
            )
        missing, changed = settlePlaybackPrepareTransaction(
            "restore-prepare-context",
            context["epoch"],
            "different-prepare-intent",
            "failed",
            dict(negative, intentId="different-prepare-intent"),
            1000,
            error_code="restore_in_progress",
            allow_restore_pending_cleanup=True,
        )
        self.assertIsNone(missing)
        self.assertFalse(changed)
        self.assertEqual(
            getPlaybackPrepareTransaction(
                "restore-prepare-context",
                context["epoch"],
                "restore-prepare-intent",
            )["status"],
            "preparing",
        )

        terminal, changed = settlePlaybackPrepareTransaction(
            "restore-prepare-context",
            context["epoch"],
            "restore-prepare-intent",
            "failed",
            negative,
            1001,
            error_code="restore_in_progress",
            allow_restore_pending_cleanup=True,
        )
        self.assertTrue(changed)
        self.assertEqual(terminal["status"], "failed")
        replay, changed = settlePlaybackPrepareTransaction(
            "restore-prepare-context",
            context["epoch"],
            "restore-prepare-intent",
            "failed",
            negative,
            1002,
            error_code="restore_in_progress",
            allow_restore_pending_cleanup=True,
        )
        self.assertFalse(changed)
        self.assertEqual(replay, terminal)
        self.assertEqual(
            getPlaybackContextState("restore-prepare-context"),
            context_before,
        )
        self.assertEqual(
            db.EmoBroadcastFence.select()
            .where(
                db.EmoBroadcastFence.resource_key
                == "restore-prepare-fence"
            )
            .count(),
            1,
        )

    def test_restore_cleanup_never_bypasses_active_or_waiting_fence(self):
        for phase in ("active", "waitingForSource"):
            with self.subTest(phase=phase):
                context_id = "restore-cleanup-%s" % phase
                client_id = "player-%s" % phase
                device_session_id = "device:%s" % client_id
                context = createStrictPlaybackContextState(
                    context_id,
                    "alice",
                    client_id,
                    device_session_id,
                    ["song-1"],
                    0,
                    0,
                    "paused",
                ).canonical_context
                createPlaybackPrepareTransaction(
                    context_id,
                    "alice",
                    context["epoch"],
                    "intent-%s" % phase,
                    "controller-1",
                    client_id,
                    device_session_id,
                    "player-nonce-1",
                    1,
                    {},
                    context["controlVersion"],
                    11000,
                )
                db.EmoBroadcastFence.create(
                    resource_key="restore-cleanup-fence-%s" % phase,
                    broadcast_id="restore-cleanup-broadcast-%s" % phase,
                    user_name="alice",
                    role="ordinary",
                    phase=phase,
                    playback_context_id=context_id,
                    client_id=client_id,
                    device_session_id=device_session_id,
                )
                with self.assertRaises(PlaybackContextBroadcastBarrierError):
                    settlePlaybackPrepareTransaction(
                        context_id,
                        context["epoch"],
                        "intent-%s" % phase,
                        "failed",
                        {
                            "playbackContextId": context_id,
                            "intentId": "intent-%s" % phase,
                            "ready": False,
                            "errorCode": "restore_in_progress",
                            "controlVersion": context["controlVersion"],
                        },
                        1000,
                        error_code="restore_in_progress",
                        allow_restore_pending_cleanup=True,
                    )
                self.assertEqual(
                    getPlaybackPrepareTransaction(
                        context_id,
                        context["epoch"],
                        "intent-%s" % phase,
                    )["status"],
                    "preparing",
                )

    def test_restore_pending_handoff_cleanup_preserves_context_and_gate(self):
        context = createStrictPlaybackContextState(
            "restore-handoff-context",
            "alice",
            "source-1",
            "device:source-1",
            ["song-1"],
            0,
            0,
            "playing",
        ).canonical_context
        db.EmoBroadcastFence.create(
            resource_key="restore-handoff-fence",
            broadcast_id="restore-handoff-broadcast",
            user_name="alice",
            role="ordinary",
            phase="restorePending",
            playback_context_id="restore-handoff-context",
            client_id="target-1",
            device_session_id="device:target-1",
        )

        def create_handoff(handoff_id):
            db.EmoPlaybackHandoff.create(
                handoff_id=handoff_id,
                playback_context_id="restore-handoff-context",
                user_name="alice",
                source_client_id="source-1",
                source_device_session_id="device:source-1",
                source_connection_nonce="source-nonce-1",
                source_connection_epoch=1,
                target_client_id="target-1",
                target_device_session_id="device:target-1",
                target_connection_nonce="target-nonce-1",
                target_connection_epoch=1,
                status="preparing",
                base_control_version=context["controlVersion"],
                context_epoch=context["epoch"],
                snapshot_json="{}",
            )

        create_handoff("restore-negative-handoff")
        context_before = getPlaybackContextState("restore-handoff-context")
        with self.assertRaises(PlaybackContextRestoreInProgressError):
            terminateStrictPlaybackHandoff(
                "restore-handoff-context",
                "restore-negative-handoff",
                "alice",
                "failed",
                error_code="source_changed",
            )
        with self.assertRaisesRegex(ValueError, "restore_in_progress"):
            terminateStrictPlaybackHandoff(
                "restore-handoff-context",
                "restore-negative-handoff",
                "alice",
                "failed",
                error_code="source_changed",
                allow_restore_pending_cleanup=True,
            )

        with self.assertRaisesRegex(ValueError, "only permits"):
            terminateStrictPlaybackHandoff(
                "restore-handoff-context",
                "restore-negative-handoff",
                "alice",
                "timed_out",
                error_code="commit_timeout",
                allow_restore_pending_cleanup=True,
            )
        self.assertEqual(
            getPlaybackHandoff("restore-negative-handoff")["status"],
            "preparing",
        )

        terminal, changed = terminateStrictPlaybackHandoff(
            "restore-handoff-context",
            "restore-negative-handoff",
            "alice",
            "failed",
            error_code="restore_in_progress",
            allow_restore_pending_cleanup=True,
        )
        self.assertTrue(changed)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["errorCode"], "restore_in_progress")

        create_handoff("restore-cancel-handoff")
        cancelled, changed = terminateStrictPlaybackHandoff(
            "restore-handoff-context",
            "restore-cancel-handoff",
            "alice",
            "cancelled",
            allow_restore_pending_cleanup=True,
        )
        self.assertTrue(changed)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(
            getPlaybackContextState("restore-handoff-context"),
            context_before,
        )
        self.assertEqual(
            db.EmoBroadcastFence.select()
            .where(
                db.EmoBroadcastFence.resource_key
                == "restore-handoff-fence"
            )
            .count(),
            1,
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

    def test_startup_recovery_terminalizes_roots_and_dependency_chains_atomically(self):
        createStrictPlaybackContextState(
            "context-1",
            "alice",
            "player-1",
            "device:player-1",
            ["song-1", "song-2", "song-3"],
            0,
            250,
            "playing",
        )
        saveDevicePlaybackState(
            "context-1",
            "device:player-1",
            "alice",
            "player-1",
            {
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 250,
                "epoch": 1,
                "appliedControlVersion": 1,
                "clientSeq": 7,
            },
            is_authority=True,
        )
        self._create_startup_recovery_chain()
        createPlaybackControlTransaction(
            "context-legacy",
            "alice",
            1,
            2,
            "legacy-controller",
            "legacy-player",
            "device:legacy-player",
            "legacy-routed-nonce",
            1,
            "player.pause",
            {"state": "paused"},
            1200,
            100,
        )
        createPlaybackControlTransaction(
            "context-terminal",
            "alice",
            1,
            2,
            "terminal-controller",
            "terminal-player",
            "device:terminal-player",
            "terminal-routed-nonce",
            1,
            "player.pause",
            {"state": "paused"},
            1000,
            100,
            requesting_device_session_id="device:terminal-controller",
            requesting_connection_nonce="terminal-requester-nonce",
            requesting_connection_epoch=1,
        )
        markPlaybackControlTransactionExecutionEligible(
            "context-terminal",
            1,
            2,
            1600,
        )
        terminal_before = settlePlaybackControlTransaction(
            "context-terminal",
            1,
            2,
            "committed",
            1700,
            applied_control_version=2,
        ).transaction
        context_before = getPlaybackContextState("context-1")
        device_before = getDevicePlaybackState("context-1", "player-1")

        with mock.patch.object(
            ws_store,
            "_mark_control_transaction_record_execution_eligible",
            wraps=ws_store._mark_control_transaction_record_execution_eligible,
        ) as mark_eligible:
            result = recoverPendingPlaybackControlsForStartup(2000)

        mark_eligible.assert_not_called()
        self.assertTrue(result["mutated"])
        self.assertEqual(
            [
                (item["playbackContextId"], item["commandControlVersion"])
                for item in result["recoveredTransactions"]
            ],
            [
                ("context-1", 2),
                ("context-1", 3),
                ("context-1", 4),
                ("context-legacy", 2),
            ],
        )
        root = getPlaybackControlTransaction("context-1", 1, 2)
        first_dependent = getPlaybackControlTransaction("context-1", 1, 3)
        second_dependent = getPlaybackControlTransaction("context-1", 1, 4)
        legacy = getPlaybackControlTransaction("context-legacy", 1, 2)
        self.assertEqual((root["status"], root["errorCode"]), ("failed", "execution_unknown"))
        self.assertGreaterEqual(root["terminalAtMs"], root["executionEligibleAtMs"])
        self.assertEqual(
            (
                first_dependent["status"],
                first_dependent["errorCode"],
                first_dependent["dependsOnControlVersion"],
            ),
            ("failed", "dependency_failed", 2),
        )
        self.assertEqual(
            (
                second_dependent["status"],
                second_dependent["errorCode"],
                second_dependent["dependsOnControlVersion"],
            ),
            ("failed", "dependency_failed", 3),
        )
        for dependent in (first_dependent, second_dependent):
            self.assertNotIn("executionEligibleAtMs", dependent)
            self.assertNotIn("watchdogDeadlineAtMs", dependent)
        self.assertEqual(
            (legacy["status"], legacy["errorCode"]),
            ("failed", "execution_unknown"),
        )
        self.assertNotIn("requestingDeviceSessionId", legacy)
        self.assertEqual(
            getPlaybackControlTransaction("context-terminal", 1, 2),
            terminal_before,
        )
        self.assertEqual(getPlaybackContextState("context-1"), context_before)
        self.assertEqual(getDevicePlaybackState("context-1", "player-1"), device_before)
        recovery = result["recovery"]
        self.assertEqual(recovery["status"], "completed")
        self.assertEqual(recovery["pendingCount"], 4)
        self.assertEqual(recovery["incompleteGenerationCount"], 1)
        self.assertEqual(recovery["recoveredRootCount"], 2)
        self.assertEqual(recovery["recoveredDependencyCount"], 2)
        self.assertEqual(
            getCoreStartupRecovery(recovery["recoveryFingerprint"]),
            recovery,
        )

    def test_startup_recovery_is_repeatable_and_concurrent_calls_mutate_once(self):
        self._create_startup_recovery_chain()
        start = threading.Barrier(2)

        def recover(started_at_ms):
            start.wait()
            return recoverPendingPlaybackControlsForStartup(started_at_ms)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(recover, (2000, 2001)))

        self.assertEqual(sum(result["mutated"] for result in results), 1)
        self.assertEqual(
            [
                getPlaybackControlTransaction("context-1", 1, version)["errorCode"]
                for version in (2, 3, 4)
            ],
            ["execution_unknown", "dependency_failed", "dependency_failed"],
        )
        marker_count = len(listCoreStartupRecoveries())
        self.assertEqual(marker_count, 2)
        replay = recoverPendingPlaybackControlsForStartup(3000)
        self.assertFalse(replay["mutated"])
        self.assertEqual(replay["recoveredTransactions"], [])
        self.assertEqual(len(listCoreStartupRecoveries()), marker_count)

    def test_startup_recovery_marker_failure_rolls_back_every_terminal(self):
        self._create_startup_recovery_chain()
        before = {
            version: getPlaybackControlTransaction("context-1", 1, version)
            for version in (2, 3, 4)
        }

        with mock.patch.object(
            db.EmoCoreStartupRecovery,
            "create",
            side_effect=RuntimeError("injected recovery marker failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected recovery marker failure",
            ):
                recoverPendingPlaybackControlsForStartup(2000)

        self.assertEqual(listCoreStartupRecoveries(), [])
        for version in (2, 3, 4):
            self.assertEqual(
                getPlaybackControlTransaction("context-1", 1, version),
                before[version],
            )

        retry = recoverPendingPlaybackControlsForStartup(2001)
        self.assertTrue(retry["mutated"])
        self.assertEqual(retry["recovery"]["pendingCount"], 3)

    def test_core_retention_enforces_retry_window_boundary(self):
        self._create_retention_context()
        cutoff_at_ms = self.RETENTION_NOW_MS - ws_store.STRICT_CORE_RETRY_WINDOW_MS
        self._create_retention_control(2, cutoff_at_ms)
        self._create_retention_control(3, cutoff_at_ms + 1)
        self._create_retention_intent(2, cutoff_at_ms)
        self._create_retention_intent(3, cutoff_at_ms + 1)

        result = cleanupStrictPlaybackContextRetention(
            "retention-context",
            self.RETENTION_NOW_MS,
            batch_size=10,
            terminal_retention_limit=0,
            local_intent_retention_limit=0,
        )

        self.assertEqual(result["cutoffAtMs"], cutoff_at_ms)
        self.assertEqual(result["deletedControlTransactions"], 1)
        self.assertEqual(result["deletedLocalIntents"], 1)
        self.assertEqual(
            [
                record.command_control_version
                for record in db.EmoPlaybackControlTransaction.select()
            ],
            [3],
        )
        self.assertEqual(
            [record.control_version for record in db.EmoPlaybackLocalIntent.select()],
            [3],
        )

    def test_core_retention_preserves_latest_512_terminal_records(self):
        self._create_retention_context()
        oldest_at_ms = (
            self.RETENTION_NOW_MS
            - ws_store.STRICT_CORE_RETRY_WINDOW_MS
            - 10000
        )
        rows = [
            self._retention_control_values(version, oldest_at_ms + version)
            for version in range(1, 514)
        ]
        with db.db.atomic():
            db.EmoPlaybackControlTransaction.insert_many(rows).execute()

        result = cleanupStrictPlaybackContextRetention(
            "retention-context",
            self.RETENTION_NOW_MS,
            batch_size=10,
        )

        self.assertEqual(result["deletedTotal"], 1)
        remaining_versions = [
            record.command_control_version
            for record in db.EmoPlaybackControlTransaction.select().order_by(
                db.EmoPlaybackControlTransaction.command_control_version
            )
        ]
        self.assertEqual(len(remaining_versions), 512)
        self.assertEqual(remaining_versions[0], 2)
        self.assertEqual(remaining_versions[-1], 513)

    def test_reconciliation_counts_toward_combined_terminal_limit(self):
        self._create_retention_context()
        old_at_ms = (
            self.RETENTION_NOW_MS
            - ws_store.STRICT_CORE_RETRY_WINDOW_MS
            - 1000
        )
        self._create_retention_control(2, old_at_ms)
        self._create_retention_reconciliation(3, old_at_ms + 100, 1)
        self._create_retention_control(4, old_at_ms + 200)

        result = cleanupStrictPlaybackContextRetention(
            "retention-context",
            self.RETENTION_NOW_MS,
            batch_size=10,
            terminal_retention_limit=2,
            local_intent_retention_limit=0,
        )

        self.assertEqual(result["deletedControlTransactions"], 1)
        self.assertEqual(result["deletedReconciliations"], 0)
        self.assertIsNone(
            getPlaybackControlTransaction("retention-context", 1, 2)
        )
        self.assertIsNotNone(
            getPlaybackControlReconciliation("retention-context", 1, 3)
        )
        self.assertIsNotNone(
            getPlaybackControlTransaction("retention-context", 1, 4)
        )

    def test_core_retention_preserves_pending_dependencies_audits_and_legacy(self):
        self._create_retention_context()
        old_at_ms = (
            self.RETENTION_NOW_MS
            - ws_store.STRICT_CORE_RETRY_WINDOW_MS
            - 1000
        )
        self._create_retention_control(2, old_at_ms)
        self._create_retention_control(
            3,
            None,
            status="pending",
            depends_on_control_version=2,
        )
        self._create_retention_control(4, old_at_ms + 1)
        self._create_retention_reconciliation(
            5,
            self.RETENTION_NOW_MS - ws_store.STRICT_CORE_RETRY_WINDOW_MS + 1,
            4,
            trigger_command_control_version=4,
        )
        self._create_retention_control(6, old_at_ms + 2)
        self._create_retention_control(
            7,
            old_at_ms + 3,
            exact_generation=False,
        )
        db.EmoBroadcastFence.create(
            resource_key="retention-restore-fence",
            broadcast_id="broadcast-retention",
            user_name="alice",
            role="ordinary",
            phase="restorePending",
            playback_context_id="retention-context",
            client_id="player-1",
            device_session_id="device:player-1",
        )

        blocked = cleanupStrictPlaybackContextRetention(
            "retention-context",
            self.RETENTION_NOW_MS,
            batch_size=10,
            terminal_retention_limit=0,
            local_intent_retention_limit=0,
        )
        self.assertTrue(blocked["blockedByFence"])
        self.assertEqual(blocked["deletedTotal"], 0)

        db.EmoBroadcastFence.delete().execute()
        result = cleanupStrictPlaybackContextRetention(
            "retention-context",
            self.RETENTION_NOW_MS,
            batch_size=10,
            terminal_retention_limit=0,
            local_intent_retention_limit=0,
        )
        self.assertEqual(result["deletedControlTransactions"], 1)
        self.assertIsNone(
            getPlaybackControlTransaction("retention-context", 1, 6)
        )
        for version in (2, 3, 4, 7):
            self.assertIsNotNone(
                getPlaybackControlTransaction("retention-context", 1, version)
            )
        self.assertIsNotNone(
            getPlaybackControlReconciliation("retention-context", 1, 5)
        )

    def test_closed_tombstone_is_preserved_across_bounded_multi_round_cleanup(self):
        self._create_retention_context()
        closeStrictPlaybackContextState("retention-context", "alice")
        closed = db.EmoPlaybackContext.get(
            db.EmoPlaybackContext.playback_context_id == "retention-context"
        )
        closed.close_outcome_json = '{"closed":true}'
        closed.save()
        old_at_ms = (
            self.RETENTION_NOW_MS
            - ws_store.STRICT_CORE_RETRY_WINDOW_MS
            - 1000
        )
        for version in range(1, 6):
            self._create_retention_intent(version, old_at_ms + version)

        deleted_per_round = []
        preserved_per_round = []
        for _ in range(4):
            result = cleanupStrictPlaybackContextRetention(
                "retention-context",
                self.RETENTION_NOW_MS,
                batch_size=2,
                terminal_retention_limit=0,
                local_intent_retention_limit=0,
            )
            deleted_per_round.append(result["deletedTotal"])
            preserved_per_round.append(result["closeTombstonePreserved"])

        self.assertEqual(deleted_per_round, [2, 2, 1, 0])
        self.assertEqual(preserved_per_round, [True, True, True, True])
        tombstone = getPlaybackContextCloseTombstone(
            "retention-context",
            "alice",
        )
        self.assertEqual(tombstone["closeOutcome"], {"closed": True})
        self.assertEqual(db.EmoPlaybackContext.select().count(), 1)

    def test_startup_recovery_retention_keeps_live_and_referenced_markers(self):
        self._create_retention_context()
        cutoff_at_ms = self.RETENTION_NOW_MS - ws_store.STRICT_CORE_RETRY_WINDOW_MS
        linked_at_ms = cutoff_at_ms - 400
        linked_control = self._create_retention_control(
            2,
            linked_at_ms,
            status="failed",
        )
        linked_control.error_code = "execution_unknown"
        linked_control.save()

        def create_marker(name, completed_at_ms, status="completed", pending_count=0):
            return db.EmoCoreStartupRecovery.create(
                recovery_fingerprint=hashlib.sha256(name.encode("utf-8")).hexdigest(),
                status=status,
                started_at_ms=max(0, completed_at_ms - 1),
                completed_at_ms=completed_at_ms,
                pending_count=pending_count,
                incomplete_generation_count=0,
                recovered_root_count=pending_count,
                recovered_dependency_count=0,
                outcome_fingerprint=hashlib.sha256(
                    (name + "-outcome").encode("utf-8")
                ).hexdigest(),
            )

        linked = create_marker("linked", linked_at_ms, pending_count=1)
        removable = create_marker("removable", cutoff_at_ms - 300)
        recent = create_marker("recent", cutoff_at_ms + 1)
        running = create_marker("running", cutoff_at_ms - 500, status="running")

        first = cleanupCoreStartupRecoveryRetention(
            self.RETENTION_NOW_MS,
            batch_size=10,
            retention_limit=1,
        )
        self.assertEqual(first["deletedStartupRecoveries"], 1)
        self.assertIsNone(
            db.EmoCoreStartupRecovery.get_or_none(
                db.EmoCoreStartupRecovery.id == removable.id
            )
        )
        for marker in (linked, recent, running):
            self.assertIsNotNone(
                db.EmoCoreStartupRecovery.get_or_none(
                    db.EmoCoreStartupRecovery.id == marker.id
                )
            )

        cleanupStrictPlaybackContextRetention(
            "retention-context",
            self.RETENTION_NOW_MS,
            batch_size=10,
            terminal_retention_limit=0,
            local_intent_retention_limit=0,
        )
        second = cleanupCoreStartupRecoveryRetention(
            self.RETENTION_NOW_MS,
            batch_size=10,
            retention_limit=1,
        )
        self.assertEqual(second["deletedStartupRecoveries"], 1)
        self.assertIsNone(
            db.EmoCoreStartupRecovery.get_or_none(
                db.EmoCoreStartupRecovery.id == linked.id
            )
        )

    def test_core_retention_rollback_restores_every_record(self):
        self._create_retention_context()
        old_at_ms = (
            self.RETENTION_NOW_MS
            - ws_store.STRICT_CORE_RETRY_WINDOW_MS
            - 1000
        )
        self._create_retention_control(
            2,
            old_at_ms,
            reconciled_by_control_version=3,
        )
        self._create_retention_reconciliation(
            3,
            old_at_ms + 1,
            2,
            trigger_command_control_version=2,
        )
        self._create_retention_intent(4, old_at_ms + 2)

        with mock.patch.object(
            db.EmoPlaybackLocalIntent,
            "delete",
            side_effect=RuntimeError("injected retention failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected retention failure"):
                cleanupStrictPlaybackContextRetention(
                    "retention-context",
                    self.RETENTION_NOW_MS,
                    batch_size=10,
                    terminal_retention_limit=0,
                    local_intent_retention_limit=0,
                )

        self.assertEqual(db.EmoPlaybackControlTransaction.select().count(), 1)
        self.assertEqual(db.EmoPlaybackControlReconciliation.select().count(), 1)
        self.assertEqual(db.EmoPlaybackLocalIntent.select().count(), 1)
        self.assertEqual(
            getPlaybackControlTransaction("retention-context", 1, 2)[
                "reconciledByControlVersion"
            ],
            3,
        )

    def test_core_retention_is_idempotent_and_serializes_concurrent_cleanup(self):
        self._create_retention_context()
        old_at_ms = (
            self.RETENTION_NOW_MS
            - ws_store.STRICT_CORE_RETRY_WINDOW_MS
            - 1000
        )
        for version in range(1, 6):
            self._create_retention_intent(version, old_at_ms + version)
        start = threading.Barrier(2)

        def cleanup(_index):
            start.wait()
            return cleanupStrictPlaybackContextRetention(
                "retention-context",
                self.RETENTION_NOW_MS,
                batch_size=10,
                terminal_retention_limit=0,
                local_intent_retention_limit=0,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(cleanup, range(2)))

        self.assertEqual(sum(result["deletedTotal"] for result in results), 5)
        self.assertEqual(
            sorted(result["deletedTotal"] for result in results),
            [0, 5],
        )
        replay = cleanupStrictPlaybackContextRetention(
            "retention-context",
            self.RETENTION_NOW_MS,
            batch_size=10,
            terminal_retention_limit=0,
            local_intent_retention_limit=0,
        )
        self.assertEqual(replay["deletedTotal"], 0)

    def test_startup_recovery_and_context_cleanup_share_context_serialization(self):
        self._create_retention_context()
        self._create_retention_control(2, None, status="pending")
        recovery_holds_context = threading.Event()
        release_recovery = threading.Event()
        cleanup_started = threading.Event()
        cleanup_finished = threading.Event()
        call_guard = threading.Lock()
        call_count = 0
        original_pending_records = ws_store._startup_recovery_pending_records

        def blocking_pending_records():
            nonlocal call_count
            records = original_pending_records()
            with call_guard:
                call_count += 1
                current_call = call_count
            if current_call == 2:
                recovery_holds_context.set()
                if not release_recovery.wait(5):
                    raise RuntimeError("retention concurrency test timed out")
            return records

        def cleanup():
            cleanup_started.set()
            try:
                return cleanupStrictPlaybackContextRetention(
                    "retention-context",
                    self.RETENTION_NOW_MS,
                    batch_size=10,
                    terminal_retention_limit=0,
                    local_intent_retention_limit=0,
                )
            finally:
                cleanup_finished.set()

        with mock.patch.object(
            ws_store,
            "_startup_recovery_pending_records",
            side_effect=blocking_pending_records,
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                recovery_future = executor.submit(
                    recoverPendingPlaybackControlsForStartup,
                    self.RETENTION_NOW_MS,
                )
                self.assertTrue(recovery_holds_context.wait(2))
                cleanup_future = executor.submit(cleanup)
                self.assertTrue(cleanup_started.wait(2))
                self.assertFalse(cleanup_finished.wait(0.05))
                release_recovery.set()
                recovery = recovery_future.result(timeout=5)
                cleanup_result = cleanup_future.result(timeout=5)

        self.assertTrue(recovery["mutated"])
        self.assertEqual(cleanup_result["deletedTotal"], 0)
        transaction = getPlaybackControlTransaction("retention-context", 1, 2)
        self.assertEqual(transaction["status"], "failed")
        self.assertEqual(transaction["errorCode"], "execution_unknown")
