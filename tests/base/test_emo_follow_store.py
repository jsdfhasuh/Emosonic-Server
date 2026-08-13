import concurrent.futures
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest import mock

from supysonic import db
from supysonic.emo import follow_store
from supysonic.emo.broadcast_store import createBroadcastState
from supysonic.emo.follow_store import (
    FollowSafetyLeaseConflictError,
    FollowSafetyLeaseLimitError,
    createFollowSafetyLease,
    getFollowSafetyLeaseForFollower,
    getFollowSafetyLeaseForSuspendedContext,
    listFollowSafetyLeases,
    listFollowSafetyLeasesForPhysicalGeneration,
    markFollowConnectionUnavailable,
    markFollowSourceContextsClosed,
    recoverFollowSafetyLeasesForStartup,
    refreshFollowSourceGeneration,
    resumeFollowSafetyLease,
    stopFollowSafetyLease,
    sweepFollowSafetyLeaseDeadlines,
)
from supysonic.emo.ws_store import (
    PlaybackContextFollowBarrierError,
    applyStrictPlaybackUpdate,
    createPlaybackPrepareTransaction,
    createStrictPlaybackContextState,
    createStrictPlaybackHandoff,
    ensureStrictPlaybackContextState,
    getDevicePlaybackState,
    getPlaybackContextState,
    saveDevicePlaybackState,
)


class EmoFollowStoreTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp()
        os.close(handle)
        db.init_database("sqlite:///" + self.db_path)
        self._create_context(
            "context-source",
            "source-1",
            "device:source-1",
        )
        self._create_context(
            "context-suspended",
            "follower-1",
            "device:follower-1",
        )

    def tearDown(self):
        db.release_database()
        os.remove(self.db_path)

    def _create_context(self, context_id, client_id, device_session_id):
        client_prefix, client_number = client_id.rsplit("-", 1)
        connection_nonce = "%s-nonce-%s" % (client_prefix, client_number)
        createStrictPlaybackContextState(
            context_id,
            "alice",
            client_id,
            device_session_id,
            ["song-1"],
            0,
            100,
            "playing",
        )
        saveDevicePlaybackState(
            context_id,
            device_session_id,
            "alice",
            client_id,
            {
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 100,
                "contextEpoch": 1,
                "appliedControlVersion": 1,
                "clientSeq": 1,
                "serverUpdatedAtMs": 1000,
                "positionSampledAtServerMs": 1000,
                "playbackRate": 1.0,
                "_connectionNonce": connection_nonce,
            },
            is_authority=True,
        )

    def _lease_values(self, **overrides):
        values = {
            "user_name": "alice",
            "follower_client_id": "follower-1",
            "follower_device_session_id": "device:follower-1",
            "follower_connection_nonce": "follower-nonce-1",
            "follower_connection_epoch": 1,
            "source_playback_context_id": "context-source",
            "source_authority_client_id": "source-1",
            "source_authority_device_session_id": "device:source-1",
            "source_connection_nonce": "source-nonce-1",
            "source_connection_epoch": 1,
            "suspended_playback_context_id": "context-suspended",
            "suspended_authority_client_id": "follower-1",
            "suspended_authority_device_session_id": "device:follower-1",
            "suspended_connection_nonce": "follower-nonce-1",
            "suspended_connection_epoch": 1,
            "start_request_fingerprint": "a" * 64,
            "server_time_ms": 1000,
        }
        values.update(overrides)
        return values

    def _create_lease(self, **overrides):
        return createFollowSafetyLease(**self._lease_values(**overrides))

    def _make_suspended_context_idle(self):
        (
            db.EmoPlaybackContext.update(
                queue_json="[]",
                current_index=0,
                track_id=None,
                state="idle",
                position_ms=0,
            )
            .where(
                db.EmoPlaybackContext.playback_context_id
                == "context-suspended"
            )
            .execute()
        )

    def _broadcast_values(self):
        snapshot = {
            "broadcastId": "broadcast-1",
            "userName": "alice",
            "playbackContextId": "context-source",
            "intentId": "intent-1",
            "ownerClientId": "controller-1",
            "authorityClientId": "source-1",
            "authorityDeviceSessionId": "device:source-1",
            "lifecycleState": "active",
            "broadcastRevision": 1,
            "queueSongIds": ["song-1"],
            "currentIndex": 0,
            "trackId": "song-1",
            "positionMs": 100,
            "state": "playing",
            "playbackRate": 1.0,
            "sourceVersion": 1,
            "sourceQueueRevision": 1,
            "sourceControlVersion": 1,
            "sourceEpoch": 1,
            "serverUpdatedAtMs": 1000,
            "participants": ["follower-1"],
        }
        participant = {
            "clientId": "follower-1",
            "deviceSessionId": "device:follower-1",
            "suspendedPlaybackContextId": "context-suspended",
            "suspendedEpoch": 1,
            "suspendedVersion": 1,
            "suspendedQueueRevision": 1,
            "suspendedControlVersion": 1,
            "suspendedAppliedControlVersion": 1,
        }
        return snapshot, participant

    def test_create_persists_exact_frozen_baseline_and_returns_isolated_copies(self):
        validator_inputs = []

        def validator(source_context, source_fact, suspended_context, suspended_fact):
            validator_inputs.append(
                (source_context, source_fact, suspended_context, suspended_fact)
            )
            source_context["queueSongIds"].append("poison")
            suspended_fact["positionMs"] = 9999

        lease, created = self._create_lease(pre_mutation_validator=validator)

        self.assertTrue(created)
        self.assertEqual(len(validator_inputs), 1)
        self.assertEqual(
            validator_inputs[0][1]["connectionNonce"],
            "source-nonce-1",
        )
        self.assertEqual(
            validator_inputs[0][3]["connectionNonce"],
            "follower-nonce-1",
        )
        self.assertEqual(lease["phase"], "active")
        self.assertEqual(lease["followerClientId"], "follower-1")
        self.assertEqual(
            lease["followerDeviceSessionId"],
            "device:follower-1",
        )
        self.assertEqual(lease["followerConnectionNonce"], "follower-nonce-1")
        self.assertEqual(lease["followerConnectionEpoch"], 1)
        self.assertEqual(lease["sourceAuthorityClientId"], "source-1")
        self.assertEqual(
            lease["sourceAuthorityDeviceSessionId"],
            "device:source-1",
        )
        self.assertEqual(lease["sourceConnectionNonce"], "source-nonce-1")
        self.assertEqual(lease["sourceConnectionEpoch"], 1)
        self.assertEqual(lease["suspendedAuthorityClientId"], "follower-1")
        self.assertEqual(
            lease["suspendedAuthorityDeviceSessionId"],
            "device:follower-1",
        )
        self.assertEqual(
            lease["suspendedConnectionNonce"],
            "follower-nonce-1",
        )
        self.assertEqual(lease["suspendedConnectionEpoch"], 1)
        self.assertEqual(
            lease["startAck"],
            {
                "action": "follow.start",
                "status": "active",
                "sourcePlaybackContextId": "context-source",
                "suspendedPlaybackContextId": "context-suspended",
                "suspendedAuthorityClientId": "follower-1",
                "suspendedAuthorityDeviceSessionId": "device:follower-1",
                "suspendedEpoch": 1,
                "suspendedVersion": 1,
                "suspendedQueueRevision": 1,
                "suspendedControlVersion": 1,
                "suspendedAppliedControlVersion": 1,
            },
        )
        self.assertNotIn("sid", lease)
        self.assertEqual(
            getPlaybackContextState("context-source")["queueSongIds"],
            ["song-1"],
        )
        self.assertEqual(
            getDevicePlaybackState("context-suspended", "follower-1")[
                "positionMs"
            ],
            100,
        )

        lease["followerClientId"] = "changed"
        lease["startAck"]["suspendedVersion"] = 999
        reread = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(reread["followerClientId"], "follower-1")
        self.assertEqual(reread["startAck"]["suspendedVersion"], 1)
        self.assertEqual(
            getFollowSafetyLeaseForSuspendedContext(
                "alice",
                "context-suspended",
            )["leaseId"],
            reread["leaseId"],
        )
        self.assertIsNone(
            getFollowSafetyLeaseForSuspendedContext(
                "bob",
                "context-suspended",
            )
        )

    def test_create_rejects_stale_source_or_suspended_fact_generation(self):
        for context_id, client_id, nonce_field in (
            ("context-source", "source-1", "source_connection_nonce"),
            (
                "context-suspended",
                "follower-1",
                "suspended_connection_nonce",
            ),
        ):
            with self.subTest(context_id=context_id):
                with self.assertRaises(FollowSafetyLeaseConflictError) as conflict:
                    self._create_lease(**{nonce_field: "replacement-nonce"})
                self.assertEqual(
                    conflict.exception.playback_context_id,
                    context_id,
                )
                self.assertEqual(db.EmoFollowSafetyLease.select().count(), 0)

    def test_durable_start_replay_precedes_live_context_validation(self):
        first, created = self._create_lease()
        self.assertTrue(created)
        (
            db.EmoPlaybackContext.update(version=99)
            .where(
                db.EmoPlaybackContext.playback_context_id
                == "context-suspended"
            )
            .execute()
        )

        validator = mock.Mock(side_effect=AssertionError("must not run"))
        replay, replay_created = self._create_lease(
            follower_connection_nonce="follower-nonce-2",
            source_connection_nonce="source-nonce-2",
            suspended_connection_nonce="follower-nonce-2",
            server_time_ms=2000,
            pre_mutation_validator=validator,
        )

        self.assertFalse(replay_created)
        self.assertEqual(replay, first)
        self.assertEqual(replay["startAck"]["suspendedVersion"], 1)
        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 1)
        validator.assert_not_called()

    def test_concurrent_duplicate_start_has_one_durable_creation(self):
        barrier = threading.Barrier(2)

        def create(_index):
            barrier.wait()
            return self._create_lease()[1]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            created = list(executor.map(create, (0, 1)))

        self.assertEqual(sorted(created), [False, True])
        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 1)

    def test_pair_context_occupancy_and_user_limit_fail_closed(self):
        self._create_lease()
        self._create_context(
            "context-source-2",
            "source-2",
            "device:source-2",
        )
        self._create_context(
            "context-suspended-2",
            "follower-2",
            "device:follower-2",
        )

        with self.assertRaises(FollowSafetyLeaseConflictError):
            self._create_lease(
                source_playback_context_id="context-source-2",
                source_authority_client_id="source-2",
                source_authority_device_session_id="device:source-2",
                source_connection_nonce="source-nonce-2",
                start_request_fingerprint="b" * 64,
            )
        with self.assertRaises(FollowSafetyLeaseConflictError):
            self._create_lease(
                follower_client_id="follower-2",
                follower_device_session_id="device:follower-2",
                follower_connection_nonce="follower-nonce-2",
                suspended_authority_client_id="follower-2",
                suspended_authority_device_session_id="device:follower-2",
                suspended_connection_nonce="follower-nonce-2",
                start_request_fingerprint="c" * 64,
            )
        with mock.patch(
            "supysonic.emo.follow_store.MAX_USER_FOLLOW_SAFETY_LEASES",
            1,
        ):
            with self.assertRaises(FollowSafetyLeaseLimitError):
                self._create_lease(
                    follower_client_id="follower-2",
                    follower_device_session_id="device:follower-2",
                    follower_connection_nonce="follower-nonce-2",
                    source_playback_context_id="context-source-2",
                    source_authority_client_id="source-2",
                    source_authority_device_session_id="device:source-2",
                    source_connection_nonce="source-nonce-2",
                    suspended_playback_context_id="context-suspended-2",
                    suspended_authority_client_id="follower-2",
                    suspended_authority_device_session_id="device:follower-2",
                    suspended_connection_nonce="follower-nonce-2",
                    start_request_fingerprint="d" * 64,
                )
        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 1)

    def test_generation_epochs_and_nested_follow_source_fail_closed(self):
        for field_name in (
            "follower_connection_epoch",
            "source_connection_epoch",
            "suspended_connection_epoch",
        ):
            for invalid_epoch in (0, 2, True, "1", None):
                with self.subTest(
                    field_name=field_name,
                    invalid_epoch=invalid_epoch,
                ):
                    with self.assertRaises(ValueError):
                        self._create_lease(**{field_name: invalid_epoch})
        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 0)

        self._create_context(
            "context-source-2",
            "source-2",
            "device:source-2",
        )
        self._create_lease(
            follower_client_id="source-1",
            follower_device_session_id="device:source-1",
            follower_connection_nonce="source-nonce-1",
            source_playback_context_id="context-source-2",
            source_authority_client_id="source-2",
            source_authority_device_session_id="device:source-2",
            source_connection_nonce="source-nonce-2",
            suspended_playback_context_id="context-source",
            suspended_authority_client_id="source-1",
            suspended_authority_device_session_id="device:source-1",
            suspended_connection_nonce="source-nonce-1",
            start_request_fingerprint="b" * 64,
        )
        with self.assertRaisesRegex(
            FollowSafetyLeaseConflictError,
            "occupied by another Follow",
        ):
            self._create_lease(start_request_fingerprint="c" * 64)
        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 1)

    def test_validator_failure_rolls_back_without_partial_lease(self):
        before_source = getPlaybackContextState("context-source")
        before_suspended = getPlaybackContextState("context-suspended")

        class ValidatorFailure(Exception):
            pass

        def fail(*_args):
            raise ValidatorFailure("generation changed")

        with self.assertRaisesRegex(ValidatorFailure, "generation changed"):
            self._create_lease(pre_mutation_validator=fail)

        self.assertEqual(listFollowSafetyLeases("alice"), [])
        self.assertEqual(getPlaybackContextState("context-source"), before_source)
        self.assertEqual(
            getPlaybackContextState("context-suspended"),
            before_suspended,
        )

    def test_existing_prepare_handoff_and_broadcast_occupancy_blocks_follow(self):
        createPlaybackPrepareTransaction(
            "context-suspended",
            "alice",
            1,
            "prepare-intent-1",
            "controller-1",
            "follower-1",
            "device:follower-1",
            "follower-nonce-1",
            1,
            {"requestId": "prepare-request-1"},
            1,
            11000,
        )
        with self.assertRaisesRegex(
            FollowSafetyLeaseConflictError,
            "active prepare",
        ):
            self._create_lease()

        db.EmoPlaybackPrepareTransaction.delete().execute()
        self._make_suspended_context_idle()
        createStrictPlaybackHandoff(
            "context-source",
            {
                "userName": "alice",
                "sourceClientId": "source-1",
                "sourceDeviceSessionId": "device:source-1",
                "sourceConnectionNonce": "source-nonce-1",
                "sourceConnectionEpoch": 1,
                "targetClientId": "follower-1",
                "targetDeviceSessionId": "device:follower-1",
                "targetConnectionNonce": "follower-nonce-1",
                "targetConnectionEpoch": 1,
                "baseControlVersion": 1,
                "handoffId": "handoff-existing",
                "snapshot": {
                    "targetDeviceSessionId": "device:follower-1",
                },
            },
            "device:follower-1",
        )
        with self.assertRaisesRegex(
            FollowSafetyLeaseConflictError,
            "active Handoff",
        ):
            self._create_lease()

        db.EmoPlaybackHandoff.delete().execute()
        snapshot, participant = self._broadcast_values()
        createBroadcastState(
            snapshot,
            [participant],
            "broadcast-fingerprint-1",
            {"broadcastId": "broadcast-1"},
        )
        with self.assertRaisesRegex(
            FollowSafetyLeaseConflictError,
            "Broadcast fence",
        ):
            self._create_lease()

        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 0)

    def test_broadcast_source_can_also_source_follow(self):
        self._create_context(
            "context-ordinary",
            "ordinary-1",
            "device:ordinary-1",
        )
        snapshot, participant = self._broadcast_values()
        snapshot["participants"] = ["ordinary-1"]
        participant.update(
            {
                "clientId": "ordinary-1",
                "deviceSessionId": "device:ordinary-1",
                "suspendedPlaybackContextId": "context-ordinary",
            }
        )
        createBroadcastState(
            snapshot,
            [participant],
            "broadcast-source-follow-fingerprint",
            {"broadcastId": "broadcast-1"},
        )

        lease, created = self._create_lease()

        self.assertTrue(created)
        self.assertEqual(lease["sourcePlaybackContextId"], "context-source")
        self.assertEqual(db.EmoBroadcast.select().count(), 1)
        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 1)

    def test_handoff_and_follow_creation_have_one_linear_pair_order(self):
        self._make_suspended_context_idle()
        self._create_context(
            "context-follow-source",
            "source-2",
            "device:source-2",
        )
        handoff_at_write = threading.Event()
        release_handoff = threading.Event()
        follow_pair_attempted = threading.Event()
        follow_finished = threading.Event()
        handoff_errors = []
        follow_errors = []
        original_handoff_create = db.EmoPlaybackHandoff.create
        original_pair_lock = follow_store.strictAuthorityPairLockSet

        def blocking_handoff_create(*args, **kwargs):
            handoff_at_write.set()
            if not release_handoff.wait(5):
                raise AssertionError("Handoff test release was not signaled")
            return original_handoff_create(*args, **kwargs)

        @contextmanager
        def observed_pair_lock(authority_pairs):
            follow_pair_attempted.set()
            with original_pair_lock(authority_pairs):
                yield

        def create_handoff():
            try:
                createStrictPlaybackHandoff(
                    "context-source",
                    {
                        "userName": "alice",
                        "sourceClientId": "source-1",
                        "sourceDeviceSessionId": "device:source-1",
                        "sourceConnectionNonce": "source-nonce-1",
                        "sourceConnectionEpoch": 1,
                        "targetClientId": "follower-1",
                        "targetDeviceSessionId": "device:follower-1",
                        "targetConnectionNonce": "follower-nonce-1",
                        "targetConnectionEpoch": 1,
                        "baseControlVersion": 1,
                        "handoffId": "handoff-race",
                        "snapshot": {
                            "targetDeviceSessionId": "device:follower-1",
                        },
                    },
                    "device:follower-1",
                )
            except Exception as exc:
                handoff_errors.append(exc)

        def create_follow():
            try:
                self._create_lease(
                    source_playback_context_id="context-follow-source",
                    source_authority_client_id="source-2",
                    source_authority_device_session_id="device:source-2",
                    source_connection_nonce="source-nonce-2",
                )
            except Exception as exc:
                follow_errors.append(exc)
            finally:
                follow_finished.set()

        with mock.patch.object(
            db.EmoPlaybackHandoff,
            "create",
            side_effect=blocking_handoff_create,
        ), mock.patch.object(
            follow_store,
            "strictAuthorityPairLockSet",
            observed_pair_lock,
        ):
            handoff_thread = threading.Thread(target=create_handoff)
            follow_thread = threading.Thread(target=create_follow)
            handoff_thread.start()
            self.assertTrue(handoff_at_write.wait(5))
            follow_thread.start()
            self.assertTrue(follow_pair_attempted.wait(5))
            self.assertFalse(follow_finished.is_set())
            release_handoff.set()
            handoff_thread.join(5)
            follow_thread.join(5)

        self.assertFalse(handoff_thread.is_alive())
        self.assertFalse(follow_thread.is_alive())
        self.assertEqual(handoff_errors, [])
        self.assertEqual(len(follow_errors), 1)
        self.assertIsInstance(
            follow_errors[0],
            FollowSafetyLeaseConflictError,
        )
        self.assertEqual(db.EmoPlaybackHandoff.select().count(), 1)
        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 0)

    def test_startup_recovery_is_durable_repeatable_and_preserves_frozen_ack(self):
        original, _created = self._create_lease()

        first = recoverFollowSafetyLeasesForStartup(2000, reconnect_grace_ms=30000)
        self.assertEqual(first[0]["phase"], "reconnectGrace")
        self.assertEqual(first[0]["followReconnectGraceExpiresAtMs"], 32000)
        self.assertEqual(first[0]["startAck"], original["startAck"])

        repeated = recoverFollowSafetyLeasesForStartup(3000, reconnect_grace_ms=30000)
        self.assertEqual(repeated[0]["phase"], "reconnectGrace")
        self.assertEqual(repeated[0]["followReconnectGraceExpiresAtMs"], 32000)
        self.assertEqual(repeated[0]["updatedAtMs"], 2000)

        db.release_database()
        db.init_database("sqlite:///" + self.db_path)
        reloaded = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(reloaded["phase"], "reconnectGrace")
        self.assertEqual(reloaded["startAck"], original["startAck"])

        expired = recoverFollowSafetyLeasesForStartup(32000)
        self.assertEqual(expired[0]["phase"], "cleanupRequired")
        self.assertNotIn("followReconnectGraceExpiresAtMs", expired[0])
        final = recoverFollowSafetyLeasesForStartup(50000)
        self.assertEqual(final[0]["phase"], "cleanupRequired")
        self.assertEqual(final[0]["updatedAtMs"], 32000)

    def test_startup_recovery_preserves_client_acquiring_and_stop_pending_safety(self):
        self.assertEqual(recoverFollowSafetyLeasesForStartup(1000), [])

        self._create_lease()
        (
            db.EmoFollowSafetyLease.update(
                stop_request_fingerprint="b" * 64,
                source_recovery_deadline_at_ms=9000,
            )
            .where(db.EmoFollowSafetyLease.user_name == "alice")
            .execute()
        )

        recovered = recoverFollowSafetyLeasesForStartup(2000)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["phase"], "cleanupRequired")
        self.assertEqual(recovered[0]["stopRequestFingerprint"], "b" * 64)
        self.assertNotIn("followReconnectGraceExpiresAtMs", recovered[0])
        self.assertNotIn("sourceRecoveryDeadlineAtMs", recovered[0])
        self.assertIsNotNone(
            getFollowSafetyLeaseForSuspendedContext(
                "alice",
                "context-suspended",
            )
        )

    def test_connection_loss_uses_distinct_follower_and_source_deadlines(self):
        original, _created = self._create_lease()

        follower = markFollowConnectionUnavailable(
            "alice",
            "follower-1",
            "device:follower-1",
            "follower-nonce-1",
            1,
            2000,
        )
        self.assertEqual(len(follower), 1)
        self.assertEqual(follower[0]["phase"], "reconnectGrace")
        self.assertEqual(
            follower[0]["followReconnectGraceExpiresAtMs"],
            32000,
        )
        self.assertNotIn("sourceRecoveryDeadlineAtMs", follower[0])

        repeated = markFollowConnectionUnavailable(
            "alice",
            "follower-1",
            "device:follower-1",
            "follower-nonce-1",
            1,
            3000,
        )
        self.assertEqual(
            repeated[0]["followReconnectGraceExpiresAtMs"],
            32000,
        )
        self.assertEqual(repeated[0]["updatedAtMs"], 2000)

        source = markFollowConnectionUnavailable(
            "alice",
            "source-1",
            "device:source-1",
            "source-nonce-1",
            1,
            4000,
        )
        self.assertEqual(source[0]["phase"], "reconnectGrace")
        self.assertEqual(source[0]["sourceRecoveryDeadlineAtMs"], 34000)
        self.assertEqual(source[0]["startAck"], original["startAck"])
        self.assertEqual(
            listFollowSafetyLeasesForPhysicalGeneration(
                "alice",
                "source-1",
                "device:source-1",
                "source-nonce-1",
                1,
            )[0]["leaseId"],
            original["leaseId"],
        )

    def test_source_disconnect_deadline_requires_fresh_generation_fact(self):
        self._create_lease()
        disconnected = markFollowConnectionUnavailable(
            "alice",
            "source-1",
            "device:source-1",
            "source-nonce-1",
            1,
            2000,
            source_recovery_ms=100,
        )
        self.assertEqual(disconnected[0]["sourceRecoveryDeadlineAtMs"], 2100)

        self.assertEqual(
            sweepFollowSafetyLeaseDeadlines(
                2050,
                source_recovery_ms=100,
                source_freshness_ms=2000,
            ),
            [],
        )
        waiting = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(waiting["sourceRecoveryDeadlineAtMs"], 2100)

        expired = sweepFollowSafetyLeaseDeadlines(
            2100,
            source_recovery_ms=100,
            source_freshness_ms=2000,
        )
        self.assertEqual(expired[0]["reason"], "sourceRecoveryExpired")
        self.assertEqual(expired[0]["lease"]["phase"], "cleanupRequired")

    def test_reconnect_resume_updates_only_live_follower_generation(self):
        original, _created = self._create_lease()
        markFollowConnectionUnavailable(
            "alice",
            "follower-1",
            "device:follower-1",
            "follower-nonce-1",
            1,
            2000,
        )
        validator_calls = []

        resumed = resumeFollowSafetyLease(
            "alice",
            "follower-1",
            "device:follower-1",
            "follower-nonce-2",
            1,
            "context-source",
            "a" * 64,
            3000,
            pre_mutation_validator=lambda lease: validator_calls.append(lease),
        )

        self.assertEqual(len(validator_calls), 1)
        self.assertEqual(validator_calls[0]["phase"], "reconnectGrace")
        self.assertEqual(resumed["phase"], "active")
        self.assertEqual(resumed["followerConnectionNonce"], "follower-nonce-2")
        self.assertEqual(
            resumed["suspendedConnectionNonce"],
            "follower-nonce-1",
        )
        self.assertNotIn("followReconnectGraceExpiresAtMs", resumed)
        self.assertEqual(resumed["startAck"], original["startAck"])
        self.assertNotEqual(resumed["leaseFingerprint"], original["leaseFingerprint"])

        replay = resumeFollowSafetyLease(
            "alice",
            "follower-1",
            "device:follower-1",
            "follower-nonce-2",
            1,
            "context-source",
            "a" * 64,
            4000,
        )
        self.assertEqual(replay["leaseFingerprint"], resumed["leaseFingerprint"])
        self.assertEqual(replay["updatedAtMs"], 3000)

    def test_expired_reconnect_requires_cleanup_and_cannot_resume(self):
        self._create_lease()
        markFollowConnectionUnavailable(
            "alice",
            "follower-1",
            "device:follower-1",
            "follower-nonce-1",
            1,
            2000,
            reconnect_grace_ms=100,
        )
        markFollowConnectionUnavailable(
            "alice",
            "source-1",
            "device:source-1",
            "source-nonce-1",
            1,
            2050,
        )

        with self.assertRaises(FollowSafetyLeaseConflictError):
            resumeFollowSafetyLease(
                "alice",
                "follower-1",
                "device:follower-1",
                "follower-nonce-2",
                1,
                "context-source",
                "a" * 64,
                2100,
            )

        lease = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(lease["phase"], "cleanupRequired")
        self.assertNotIn("followReconnectGraceExpiresAtMs", lease)
        self.assertNotIn("sourceRecoveryDeadlineAtMs", lease)

    def test_source_idle_waits_while_stale_playing_uses_bounded_recovery(self):
        self._create_lease()
        source = db.EmoPlaybackContext.get(
            db.EmoPlaybackContext.playback_context_id == "context-source"
        )
        source.queue_json = "[]"
        source.current_index = 0
        source.track_id = None
        source.state = "idle"
        source.position_ms = 0
        source.save()

        self.assertEqual(sweepFollowSafetyLeaseDeadlines(5000), [])
        idle_lease = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(idle_lease["phase"], "active")
        self.assertNotIn("sourceRecoveryDeadlineAtMs", idle_lease)

        source.queue_json = '["song-1"]'
        source.track_id = "song-1"
        source.state = "playing"
        source.save()
        started = sweepFollowSafetyLeaseDeadlines(
            5000,
            source_recovery_ms=100,
            source_freshness_ms=2000,
        )
        self.assertEqual(started[0]["reason"], "sourceRecoveryStarted")
        self.assertEqual(
            started[0]["lease"]["sourceRecoveryDeadlineAtMs"],
            5100,
        )
        expired = sweepFollowSafetyLeaseDeadlines(
            5100,
            source_recovery_ms=100,
            source_freshness_ms=2000,
        )
        self.assertEqual(expired[0]["reason"], "sourceRecoveryExpired")
        self.assertEqual(expired[0]["lease"]["phase"], "cleanupRequired")
        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 1)

    def test_source_recovery_rejects_mismatched_or_future_physical_facts(self):
        self._create_lease()
        source = db.EmoPlaybackContext.get(
            db.EmoPlaybackContext.playback_context_id == "context-source"
        )
        device = db.EmoDevicePlaybackState.get(
            db.EmoDevicePlaybackState.playback_context_id == "context-source"
        )

        def reset_source():
            source.queue_json = '["song-1"]'
            source.current_index = 0
            source.track_id = "song-1"
            source.state = "playing"
            source.playback_json = json.dumps({"playbackRate": 1.0})
            source.save()
            device.state = "playing"
            device.track_id = "song-1"
            device.position_ms = 100
            device.is_authority = 1
            device.playback_json = json.dumps(
                {
                    "_connectionNonce": "source-nonce-1",
                    "playbackRate": 1.0,
                    "serverUpdatedAtMs": 1000,
                    "positionSampledAtServerMs": 1000,
                }
            )
            device.save()
            (
                db.EmoFollowSafetyLease.update(
                    phase="active",
                    source_recovery_deadline_at_ms=None,
                )
                .where(db.EmoFollowSafetyLease.user_name == "alice")
                .execute()
            )

        mutations = {
            "canonical queue": lambda: setattr(source, "current_index", 1),
            "track": lambda: setattr(device, "track_id", "song-other"),
            "state": lambda: setattr(device, "state", "paused"),
            "rate": lambda: setattr(
                device,
                "playback_json",
                json.dumps(
                    {
                        "_connectionNonce": "source-nonce-1",
                        "playbackRate": 1.25,
                        "serverUpdatedAtMs": 1000,
                        "positionSampledAtServerMs": 1000,
                    }
                ),
            ),
            "future time": lambda: setattr(
                device,
                "playback_json",
                json.dumps(
                    {
                        "_connectionNonce": "source-nonce-1",
                        "playbackRate": 1.0,
                        "serverUpdatedAtMs": 2001,
                        "positionSampledAtServerMs": 1000,
                    }
                ),
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                reset_source()
                mutate()
                if label == "canonical queue":
                    source.save()
                else:
                    device.save()
                transitions = sweepFollowSafetyLeaseDeadlines(
                    2000,
                    source_recovery_ms=100,
                )
                self.assertEqual(len(transitions), 1)
                self.assertEqual(
                    transitions[0]["reason"],
                    "sourceRecoveryStarted",
                )

        reset_source()
        source.state = "paused"
        source.save()
        device.state = "paused"
        device.save()
        self.assertEqual(sweepFollowSafetyLeaseDeadlines(5000), [])

    def test_source_generation_refresh_and_close_preserve_fence(self):
        original, _created = self._create_lease()
        markFollowConnectionUnavailable(
            "alice",
            "source-1",
            "device:source-1",
            "source-nonce-1",
            1,
            2000,
        )

        refreshed = refreshFollowSourceGeneration(
            "alice",
            "context-source",
            "source-1",
            "device:source-1",
            "source-nonce-2",
            1,
            3000,
        )
        self.assertEqual(refreshed[0]["sourceConnectionNonce"], "source-nonce-2")
        self.assertNotIn("sourceRecoveryDeadlineAtMs", refreshed[0])
        self.assertNotEqual(refreshed[0]["leaseFingerprint"], original["leaseFingerprint"])

        closed = markFollowSourceContextsClosed(("context-source",), 4000)
        self.assertEqual(closed[0]["phase"], "cleanupRequired")
        self.assertIsNotNone(
            getFollowSafetyLeaseForSuspendedContext(
                "alice",
                "context-suspended",
            )
        )

    def test_exact_pair_stop_deletes_lease_and_rolls_back_delete_failure(self):
        self._create_lease()
        with self.assertRaises(FollowSafetyLeaseConflictError):
            stopFollowSafetyLease(
                "alice",
                "follower-1",
                "device:follower-1",
                "context-other",
                "b" * 64,
                2000,
            )
        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 1)

        with mock.patch.object(
            follow_store.EmoFollowSafetyLease,
            "delete_instance",
            side_effect=RuntimeError("injected stop delete failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected stop delete failure"):
                stopFollowSafetyLease(
                    "alice",
                    "follower-1",
                    "device:follower-1",
                    "context-source",
                    "b" * 64,
                    2000,
                )
        rolled_back = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertIsNotNone(rolled_back)
        self.assertNotIn("stopRequestFingerprint", rolled_back)

        with mock.patch.object(
            follow_store,
            "MAX_USER_FOLLOW_SAFETY_LEASES",
            0,
        ):
            stopped = stopFollowSafetyLease(
                "alice",
                "follower-1",
                "device:follower-1",
                "context-source",
                "b" * 64,
                3000,
            )
        self.assertEqual(stopped["phase"], "active")
        self.assertIn("stopRequestFingerprint", stopped)
        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 0)
        self.assertIsNone(
            stopFollowSafetyLease(
                "alice",
                "follower-1",
                "device:follower-1",
                "context-source",
                "b" * 64,
                4000,
            )
        )

    def test_suspended_context_is_fenced_while_source_context_remains_mutable(self):
        self._create_lease()
        suspended_context = getPlaybackContextState("context-suspended")
        suspended_device = getDevicePlaybackState("context-suspended", "follower-1")
        payload = {
            "playbackContextId": "context-suspended",
            "deviceSessionId": "device:follower-1",
            "origin": "passive",
            "appliedControlVersion": 1,
            "state": "playing",
            "trackId": "song-1",
            "positionMs": 200,
            "positionSampledAtServerMs": 2000,
            "playbackRate": 1.0,
            "clientSeq": 2,
        }

        with self.assertRaises(PlaybackContextFollowBarrierError):
            applyStrictPlaybackUpdate(
                "context-suspended",
                "alice",
                "follower-1",
                "device:follower-1",
                "follower-nonce-1",
                payload,
                2000,
            )
        self.assertEqual(
            getPlaybackContextState("context-suspended"),
            suspended_context,
        )
        self.assertEqual(
            getDevicePlaybackState("context-suspended", "follower-1"),
            suspended_device,
        )

        payload.update(
            playbackContextId="context-source",
            deviceSessionId="device:source-1",
        )
        result = applyStrictPlaybackUpdate(
            "context-source",
            "alice",
            "source-1",
            "device:source-1",
            "source-nonce-1",
            payload,
            2000,
        )
        self.assertEqual(result["canonicalUpdate"]["positionMs"], 200)

        self._create_context(
            "context-broadcast-participant",
            "participant-2",
            "device:participant-2",
        )
        snapshot, participant = self._broadcast_values()
        snapshot.update(
            broadcastId="broadcast-follow-source",
            intentId="intent-follow-source",
            participants=["participant-2"],
        )
        participant.update(
            clientId="participant-2",
            deviceSessionId="device:participant-2",
            suspendedPlaybackContextId="context-broadcast-participant",
        )
        broadcast = createBroadcastState(
            snapshot,
            [participant],
            "broadcast-follow-source-fingerprint",
            {"broadcastId": "broadcast-follow-source"},
        )
        self.assertTrue(broadcast["created"])

    def test_ensure_handoff_and_broadcast_respect_durable_occupancy(self):
        self._create_lease()

        with self.assertRaises(PlaybackContextFollowBarrierError):
            ensureStrictPlaybackContextState(
                "alice",
                "follower-1",
                "device:follower-replacement",
                ["song-1"],
                0,
                0,
                "playing",
            )
        with self.assertRaises(PlaybackContextFollowBarrierError):
            createStrictPlaybackHandoff(
                "context-source",
                {
                    "userName": "alice",
                    "sourceClientId": "source-1",
                    "sourceDeviceSessionId": "device:source-1",
                    "sourceConnectionNonce": "source-nonce-1",
                    "sourceConnectionEpoch": 1,
                    "targetClientId": "follower-1",
                    "targetDeviceSessionId": "device:follower-1",
                    "targetConnectionNonce": "follower-nonce-1",
                    "targetConnectionEpoch": 1,
                    "baseControlVersion": 1,
                    "handoffId": "handoff-1",
                },
                "device:follower-1",
            )

        snapshot, participant = self._broadcast_values()
        with self.assertRaises(PlaybackContextFollowBarrierError):
            createBroadcastState(
                snapshot,
                [participant],
                "broadcast-fingerprint-1",
                {"broadcastId": "broadcast-1"},
            )
        self.assertEqual(db.EmoPlaybackHandoff.select().count(), 0)
        self.assertEqual(db.EmoBroadcast.select().count(), 0)


if __name__ == "__main__":
    unittest.main()
