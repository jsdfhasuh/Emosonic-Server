import concurrent.futures
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
    recoverFollowSafetyLeasesForStartup,
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
                "targetClientId": "follower-1",
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
                        "targetClientId": "follower-1",
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
                    "targetClientId": "follower-1",
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
