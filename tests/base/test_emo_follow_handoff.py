"""Real Socket.IO + isolated SQLite regressions for Context-driven Follow."""

import json
import subprocess
import sys
import threading
from unittest import mock

from supysonic import db
from supysonic.emo import follow_store
from supysonic.emo import ws as emo_ws
from supysonic.emo.ws_state import get_state
from supysonic.emo.ws_store import getPlaybackContextState, getPlaybackHandoff
from tests.base import test_emo_strict_v2_handoff as handoff_tests
from tests.base import test_emo_ws as ws_tests


class FollowHandoffContinuityTestCase(ws_tests.EmoWebSocketTestCase):
    connect_handoff_devices = handoff_tests.StrictV2HandoffTestCase.connect_handoff_devices
    start_handoff = handoff_tests.StrictV2HandoffTestCase.start_handoff
    complete_handoff = handoff_tests.StrictV2HandoffTestCase.complete_handoff

    def _connect_follower(self, suffix="1"):
        client_id = "follower-" + suffix
        device_id = "device:" + client_id
        context_id = "context-suspended-" + suffix
        follower = self.connect_device(
            "alice", "Alic3", client_id, device_id, ["player"],
            capabilities={
                ws_tests.CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.ensure_playback_context(
            follower, "ensure-" + suffix, playback_context_id=context_id,
            device_session_id=device_id, queue_song_ids=["own-song"],
            state="stopped", position_ms=300,
        )
        self.report_strict_playback_context(
            follower, "fact-" + suffix, context_id, device_id,
        )
        self.get_messages(follower)
        follower.emit("message", {
            "type": "command", "action": "follow.start",
            "requestId": "follow-start-" + suffix,
            "payload": {"sourcePlaybackContextId": "context-handoff-1",
                        "deviceSessionId": device_id},
        }, namespace="/emo")
        self.get_ack(self.get_messages(follower), "follow-start-" + suffix)
        return follower, self._lease(suffix)

    def _lease(self, suffix="1"):
        return follow_store.getFollowSafetyLeaseForFollower(
            "alice", "follower-" + suffix, "device:follower-" + suffix,
        )

    def _commit(self, target, controller):
        ack = self.get_ack(self.start_handoff(controller), "handoff-start-1")
        target.emit("message", {
            "type": "event", "action": "playback.ready",
            "requestId": "handoff-ready-1",
            "payload": {
                "playbackContextId": "context-handoff-1",
                "handoffId": ack["payload"]["handoffId"],
                "prepareId": ack["payload"]["prepareId"],
                "deviceSessionId": "device:target-1", "ready": True,
            },
        }, namespace="/emo")
        commit = next(m for m in self.get_messages(target)
                      if m["action"] == "player.play")
        return ack["payload"]["handoffId"], commit

    def _assert_migrated(self, before, suffix="1"):
        after = self._lease(suffix)
        target_generation = get_state().get_current_physical_generation(
            "alice", "target-1", "device:target-1",
        )
        self.assertEqual(after["sourceAuthorityClientId"], "target-1")
        self.assertEqual(after["sourceAuthorityDeviceSessionId"], "device:target-1")
        self.assertEqual(after["sourceConnectionNonce"], target_generation["connectionNonce"])
        self.assertEqual(after["sourceConnectionEpoch"], target_generation["connectionEpoch"])
        self.assertNotIn("sourceRecoveryDeadlineAtMs", after)
        self.assertNotEqual(after["leaseFingerprint"], before["leaseFingerprint"])
        changed = {"sourceAuthorityClientId", "sourceAuthorityDeviceSessionId",
                   "sourceConnectionNonce", "sourceConnectionEpoch", "leaseFingerprint",
                   "updatedAtMs", "sourceRecoveryDeadlineAtMs"}
        self.assertEqual({k: v for k, v in before.items() if k not in changed},
                         {k: v for k, v in after.items() if k not in changed})
        return after

    def test_follow_handoff_completion_preserves_multiple_followers_and_fences(self):
        source, target, controller = self.connect_handoff_devices()
        follower, before = self._connect_follower()
        _, second = self._connect_follower("2")
        suspended = getPlaybackContextState("context-suspended-1")
        handoff_id, commit = self._commit(target, controller)
        self.assertEqual(self._lease(), before)
        self.complete_handoff(target, handoff_id, commit, "complete-1")
        after = self._assert_migrated(before)
        self._assert_migrated(second, "2")
        follower_statuses = [
            m["payload"]["playbackContext"] for m in self.get_messages(follower)
            if m["action"] == "playback.context.status"
        ]
        self.assertTrue(any(
            status["authorityClientId"] == "target-1"
            and status["authorityDeviceSessionId"] == "device:target-1"
            for status in follower_statuses
        ))
        self.assertEqual(getPlaybackContextState("context-suspended-1"), suspended)
        relationship = get_state().get_follow_relationship("follower-1")
        self.assertEqual(relationship["sourceClientId"], "target-1")
        self.assertTrue(relationship["active"])
        now_ms = commit["payload"]["effectiveAtServerMs"]
        self.assertEqual(emo_ws._sweep_follow_safety_leases(now_ms), [])
        # Old physical source disconnects must not start recovery for the new source.
        self.assertEqual(follow_store.markFollowConnectionUnavailable(
            "alice", "source-1", "device:source-1",
            before["sourceConnectionNonce"], before["sourceConnectionEpoch"], now_ms,
        ), [])
        self.assertEqual(self._lease(), after)
        self.get_messages(source)
        self.get_messages(target)
        with mock.patch.object(emo_ws, "_server_time_ms", return_value=now_ms + 1):
            old_messages = self.report_strict_playback_context(
                source, "old-source-fact", "context-handoff-1", "device:source-1",
                position_ms=1500, client_seq=2,
            )
            new_messages = self.report_strict_playback_context(
                target, "new-source-fact", "context-handoff-1", "device:target-1",
                position_ms=commit["payload"]["positionMs"], client_seq=2,
            )
            fenced = self.report_strict_playback_context(
                follower, "follower-write", "context-suspended-1", "device:follower-1",
                client_seq=2,
            )
        self.assertTrue(any(m["action"] == "system.error" for m in old_messages))
        self.assertFalse(any(m["action"] == "system.error" for m in new_messages))
        self.assertTrue(any(m["action"] == "system.error" for m in fenced))
        self.assertEqual(emo_ws._sweep_follow_safety_leases(now_ms + 2), [])

    def test_follow_handoff_complete_replay_is_noop_for_lease(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        handoff_id, commit = self._commit(target, controller)
        self.complete_handoff(target, handoff_id, commit, "complete-1")
        after = self._assert_migrated(before)
        self.complete_handoff(target, handoff_id, commit, "complete-2")
        self.assertEqual(self._lease(), after)
        self.complete_handoff(target, handoff_id, commit, "complete-conflict", client_seq=2)
        self.assertEqual(self._lease(), after)

    def test_follow_handoff_persists_new_source_and_restart_keeps_cleanup_fence(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        handoff_id, commit = self._commit(target, controller)
        self.complete_handoff(target, handoff_id, commit, "complete-1")
        after = self._assert_migrated(before)
        get_state()._follow_relationships.clear()
        # A fresh interpreter reads the same isolated on-disk DB and performs
        # startup recovery; no in-process lease cache can provide these facts.
        recovered = json.loads(subprocess.check_output([
            sys.executable, "-c",
            "import json,sys; from supysonic import db; "
            "from supysonic.emo.follow_store import recoverFollowSafetyLeasesForStartup; "
            "db.init_database('sqlite:///' + sys.argv[1]); "
            "print(json.dumps(recoverFollowSafetyLeasesForStartup(int(sys.argv[2])))); "
            "db.release_database()",
            db.db.database, str(commit["payload"]["effectiveAtServerMs"] + 1),
        ], text=True))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["sourceAuthorityClientId"], "target-1")
        self.assertEqual(recovered[0]["startAck"], before["startAck"])
        self.assertEqual(recovered[0]["sourceConnectionNonce"], after["sourceConnectionNonce"])
        self.assertEqual(recovered[0]["leaseFingerprint"], after["leaseFingerprint"])
        self.assertEqual(recovered[0]["phase"], "reconnectGrace")
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))
        self.assertIsNotNone(follow_store.getFollowSafetyLeaseForSuspendedContext(
            "alice", "context-suspended-1",
        ))

    def test_follow_handoff_db_failure_rolls_back_authority_standby_and_all_leases(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        _, second = self._connect_follower("2")
        handoff_id, commit = self._commit(target, controller)
        canonical = getPlaybackContextState("context-handoff-1")
        standby = getPlaybackContextState("context-handoff-target-idle")
        save_lease = follow_store._save_lease

        def fail_after_write(record, server_time_ms):
            save_lease(record, server_time_ms)
            raise RuntimeError("injected persistence failure after lease write")

        with mock.patch.object(follow_store, "_save_lease", side_effect=fail_after_write):
            messages = self.complete_handoff(target, handoff_id, commit, "complete-failed")
        self.assertTrue(any(m["action"] == "system.error" for m in messages))
        self.assertEqual(getPlaybackContextState("context-handoff-1"), canonical)
        self.assertEqual(getPlaybackContextState("context-handoff-target-idle"), standby)
        self.assertEqual(getPlaybackHandoff(handoff_id)["status"], "committed")
        self.assertEqual(self._lease(), before)
        self.assertEqual(self._lease("2"), second)
        self.assertEqual(get_state().get_follow_relationship("follower-1")["sourceClientId"], "source-1")
        self.complete_handoff(target, handoff_id, commit, "complete-retry")
        self._assert_migrated(before)
        self._assert_migrated(second, "2")

    def test_follow_handoff_reconnect_grace_and_cleanup_are_not_reactivated(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        _, cleanup = self._connect_follower("2")
        now_ms = emo_ws._server_time_ms()
        follow_store.markFollowConnectionUnavailable(
            "alice", "follower-1", "device:follower-1",
            before["followerConnectionNonce"], before["followerConnectionEpoch"], now_ms,
        )
        get_state().deactivate_follow_relationships_for_follower("follower-1")
        before = self._lease()
        db.EmoFollowSafetyLease.update(phase="cleanupRequired").where(
            db.EmoFollowSafetyLease.id == cleanup["leaseId"],
        ).execute()
        cleanup = self._lease("2")
        handoff_id, commit = self._commit(target, controller)
        self.complete_handoff(target, handoff_id, commit, "complete-1")
        after = self._assert_migrated(before)
        self.assertEqual(after["phase"], "reconnectGrace")
        self.assertEqual(after["followReconnectGraceExpiresAtMs"], before["followReconnectGraceExpiresAtMs"])
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))
        self.assertEqual(self._lease("2"), cleanup)

    def test_follow_handoff_frozen_source_identity_is_required_for_migration(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        excluded = []
        for suffix, updates in enumerate((
            {"user_name": "bob"},
            {"source_playback_context_id": "unrelated-context"},
            {"source_authority_client_id": "unrelated-source"},
            {"source_authority_device_session_id": "device:old-source"},
            {"source_connection_nonce": "stale-source-nonce"},
            {"source_connection_epoch": 2},
        ), start=2):
            _, lease = self._connect_follower(str(suffix))
            db.EmoFollowSafetyLease.update(**updates).where(
                db.EmoFollowSafetyLease.id == lease["leaseId"],
            ).execute()
            record = db.EmoFollowSafetyLease.get_by_id(lease["leaseId"])
            excluded.append((record.id, follow_store.serializeFollowSafetyLease(record)))
        handoff_id, commit = self._commit(target, controller)
        self.complete_handoff(target, handoff_id, commit, "complete-1")
        self._assert_migrated(before)
        for lease_id, frozen in excluded:
            with self.subTest(lease_id=lease_id):
                self.assertEqual(follow_store.serializeFollowSafetyLease(
                    db.EmoFollowSafetyLease.get_by_id(lease_id)), frozen)

    def test_follow_handoff_cancel_and_late_complete_do_not_transfer(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        handoff_id, commit = self._commit(target, controller)
        controller.emit("message", {
            "type": "command", "action": "playback.handoff.cancel",
            "requestId": "cancel-handoff", "payload": {
                "playbackContextId": "context-handoff-1", "handoffId": handoff_id,
                "reason": "user_cancelled",
            },
        }, namespace="/emo")
        self.get_ack(self.get_messages(controller), "cancel-handoff")
        self.complete_handoff(target, handoff_id, commit, "late-complete")
        self.assertEqual(self._lease(), before)
        self.assertEqual(getPlaybackContextState("context-handoff-1")["authorityClientId"], "source-1")
        self.assertEqual(getPlaybackHandoff(handoff_id)["status"], "cancelled")

    def test_follow_handoff_timeout_and_late_complete_do_not_transfer(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        handoff_id, commit = self._commit(target, controller)
        deadline = getPlaybackHandoff(handoff_id)["completeExpiresAtMs"]
        self.complete_handoff(target, handoff_id, commit, "expired-complete",
                              server_received_at_ms=deadline)
        self.assertEqual(self._lease(), before)
        self.assertEqual(getPlaybackHandoff(handoff_id)["status"], "timed_out")
        self.assertEqual(getPlaybackContextState("context-handoff-1")["authorityClientId"], "source-1")

    def test_follow_handoff_invalid_complete_proof_does_not_transfer(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        handoff_id, commit = self._commit(target, controller)
        for suffix, override in enumerate((
            {"appliedControlVersion": 1}, {"trackId": "different-track"},
            {"deviceSessionId": "device:replacement"},
            {"positionSampledAtServerMs": 1},
        )):
            messages = self.complete_handoff(target, handoff_id, commit,
                                             "invalid-" + str(suffix), overrides=override)
            self.assertFalse(any(m["action"] == "playback.handoff.status"
                                 and m["payload"].get("status") == "completed"
                                 for m in messages))
            self.assertEqual(self._lease(), before)
            self.assertEqual(getPlaybackContextState("context-handoff-1")["authorityClientId"], "source-1")
        self.complete_handoff(target, handoff_id, commit, "valid-complete")
        self._assert_migrated(before)

    def test_follow_handoff_current_target_updates_continue_beyond_source_recovery_window(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        handoff_id, commit = self._commit(target, controller)
        self.complete_handoff(target, handoff_id, commit, "complete-1")
        now_ms = commit["payload"]["effectiveAtServerMs"]
        safety_time = emo_ws.strict_v2_safety._time_fn()
        # Advance both clocks; keep the actual rate limiter and protocol path enabled.
        for seconds in range(1, 34):
            sample_time = now_ms + seconds * 1000
            with mock.patch.object(emo_ws, "_server_time_ms", return_value=sample_time), mock.patch.object(
                emo_ws.strict_v2_safety, "_time_fn", return_value=safety_time + seconds,
            ):
                messages = self.report_strict_playback_context(
                    target, "target-fact-" + str(seconds), "context-handoff-1",
                    "device:target-1", client_seq=seconds + 1,
                    position_ms=commit["payload"]["positionMs"] + seconds * 1000,
                )
            self.assertFalse(any(m["action"] == "system.error" for m in messages))
            self.assertEqual(emo_ws._sweep_follow_safety_leases(sample_time), [])
        self.assertEqual(self._lease()["phase"], "active")
        self.assertTrue(get_state().get_follow_relationship("follower-1")["active"])

    def test_follow_handoff_target_generation_replacement_does_not_transfer(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        handoff_id, commit = self._commit(target, controller)
        replacement = self.connect_device(
            "alice", "Alic3", "target-1", "device:target-1", ["player"],
            capabilities={ws_tests.CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                          "playbackPrepare": True, "effectiveAtPlayback": True},
        )
        self.clients.remove(target)
        self.complete_handoff(replacement, handoff_id, commit, "replacement-complete")
        self.assertEqual(self._lease(), before)
        self.assertEqual(getPlaybackContextState("context-handoff-1")["authorityClientId"], "source-1")
        self.assertNotEqual(getPlaybackHandoff(handoff_id)["status"], "completed")

    def test_follow_handoff_valid_completion_clears_source_recovery(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        handoff_id, commit = self._commit(target, controller)
        follow_store.markFollowConnectionUnavailable(
            "alice", "source-1", "device:source-1",
            before["sourceConnectionNonce"], before["sourceConnectionEpoch"],
            commit["payload"]["effectiveAtServerMs"] - 1,
        )
        before = self._lease()
        self.assertIn("sourceRecoveryDeadlineAtMs", before)
        self.complete_handoff(target, handoff_id, commit, "complete-1")
        self._assert_migrated(before)

    def test_follow_handoff_concurrent_stop_waits_for_atomic_transfer(self):
        _, target, controller = self.connect_handoff_devices()
        _, before = self._connect_follower()
        handoff_id, commit = self._commit(target, controller)
        transfer_written = threading.Event()
        allow_commit = threading.Event()
        stop_started = threading.Event()
        stop_done = threading.Event()
        errors = []
        save_lease = follow_store._save_lease

        def pause_transfer(record, server_time_ms):
            save_lease(record, server_time_ms)
            if record.source_authority_client_id == "target-1" and record.stop_request_fingerprint is None:
                transfer_written.set()
                if not allow_commit.wait(5):
                    raise RuntimeError("test did not release completion transaction")

        def complete():
            try:
                self.complete_handoff(target, handoff_id, commit, "complete-thread")
            except Exception as exc:
                errors.append(exc)

        def stop():
            try:
                stop_started.set()
                stopped = follow_store.stopFollowSafetyLease(
                    "alice", "follower-1", "device:follower-1", "context-handoff-1",
                    "b" * 64, commit["payload"]["effectiveAtServerMs"] + 1,
                )
                self.assertEqual(stopped["sourceAuthorityClientId"], "target-1")
            except Exception as exc:
                errors.append(exc)
            finally:
                stop_done.set()

        with mock.patch.object(follow_store, "_save_lease", side_effect=pause_transfer):
            complete_thread = threading.Thread(target=complete)
            stop_thread = threading.Thread(target=stop)
            complete_thread.start()
            try:
                self.assertTrue(transfer_written.wait(5))
                # Other DB connections cannot see a partial authority/lease switch.
                self.assertEqual(self._lease(), before)
                self.assertEqual(getPlaybackContextState("context-handoff-1")["authorityClientId"], "source-1")
                stop_thread.start()
                self.assertTrue(stop_started.wait(5))
                self.assertFalse(stop_done.wait(0.05))
            finally:
                allow_commit.set()
                complete_thread.join(5)
                if stop_thread.ident is not None:
                    stop_thread.join(5)
            self.assertFalse(complete_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(self._lease())
        self.assertEqual(getPlaybackHandoff(handoff_id)["status"], "completed")

    def test_follow_handoff_start_replay_and_reconnect_preserve_migrated_source(self):
        _, target, controller = self.connect_handoff_devices()
        follower, before = self._connect_follower()
        handoff_id, commit = self._commit(target, controller)
        self.complete_handoff(target, handoff_id, commit, "complete-before-replay")
        migrated = self._assert_migrated(before)

        def replay(client, request_id):
            self.get_messages(client)
            client.emit("message", {
                "type": "command", "action": "follow.start", "requestId": request_id,
                "payload": {
                    "sourcePlaybackContextId": "context-handoff-1",
                    "deviceSessionId": "device:follower-1",
                },
            }, namespace="/emo")
            ack = self.get_ack(self.get_messages(client), request_id)
            self.assertEqual(ack["payload"], before["startAck"])
            lease = self._lease()
            self.assertEqual(lease["startRequestFingerprint"], before["startRequestFingerprint"])
            self.assertEqual(lease["sourceAuthorityClientId"], "target-1")
            self.assertEqual(lease["sourceConnectionNonce"], migrated["sourceConnectionNonce"])
            self.assertEqual(get_state().get_follow_relationship("follower-1")["sourceClientId"], "target-1")
            return lease

        self.assertEqual(replay(follower, "replay-after-handoff"), migrated)
        follower.disconnect(namespace="/emo")
        self.clients.remove(follower)
        reconnect = self.connect_device(
            "alice", "Alic3", "follower-1", "device:follower-1", ["player"],
            capabilities={
                ws_tests.CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        resumed = replay(reconnect, "resume-after-handoff")
        self.assertEqual(resumed["phase"], "active")
        self.assertNotEqual(resumed["followerConnectionNonce"], before["followerConnectionNonce"])
