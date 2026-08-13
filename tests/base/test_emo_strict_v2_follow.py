import unittest
from contextlib import contextmanager
from unittest import mock

from supysonic import db
from supysonic.emo import follow_store
from supysonic.emo import ws as emo_ws
from supysonic.emo.follow_store import getFollowSafetyLeaseForFollower
from supysonic.emo.ws_state import get_state
from supysonic.emo.ws_store import (
    getDevicePlaybackState,
    getPlaybackContextState,
)

from tests.base.test_emo_ws import (
    CAPABILITY_PLAYBACK_CONTEXT_V2,
    EmoWebSocketTestCase,
)


class StrictV2FollowTestCase(EmoWebSocketTestCase):
    def prepare_context_fact(
        self,
        client,
        client_id,
        device_session_id,
        playback_context_id,
        state="stopped",
        queue_song_ids=None,
        position_ms=100,
        sampled_at_server_ms=None,
    ):
        self.ensure_playback_context(
            client,
            "ensure-%s" % playback_context_id,
            playback_context_id=playback_context_id,
            device_session_id=device_session_id,
            queue_song_ids=(
                ["song-%s" % client_id]
                if queue_song_ids is None
                else queue_song_ids
            ),
            position_ms=position_ms,
            state=state,
        )
        self.report_strict_playback_context(
            client,
            "fact-%s" % playback_context_id,
            playback_context_id,
            device_session_id,
            state=state,
            position_ms=position_ms,
            sampled_at_server_ms=sampled_at_server_ms,
        )

    def prepare_follow_pair(
        self,
        source,
        follower,
        source_client_id="owner-1",
        source_device_session_id="device:owner-1",
        follower_client_id="follower-1",
        follower_device_session_id="device:follower-1",
        source_playback_context_id="context-source-1",
        suspended_playback_context_id="context-suspended-1",
        source_state="stopped",
        source_sampled_at_server_ms=None,
    ):
        self.prepare_context_fact(
            source,
            source_client_id,
            source_device_session_id,
            source_playback_context_id,
            state=source_state,
            sampled_at_server_ms=source_sampled_at_server_ms,
        )
        self.prepare_context_fact(
            follower,
            follower_client_id,
            follower_device_session_id,
            suspended_playback_context_id,
            state="stopped",
        )
        self.get_messages(source)
        self.get_messages(follower)

    def start_follow(
        self,
        client,
        request_id="follow-start-1",
        playback_context_id="context-source-1",
        device_session_id="device:follower-1",
    ):
        client.emit(
            "message",
            {
                "type": "command",
                "action": "follow.start",
                "requestId": request_id,
                "payload": {
                    "sourcePlaybackContextId": playback_context_id,
                    "deviceSessionId": device_session_id,
                },
            },
            namespace="/emo",
        )
        return self.get_messages(client)

    def test_start_retry_and_stop_use_ack_only_settlement(self):
        owner = self.connect_device(
            "alice",
            "Alic3",
            "owner-1",
            "device:owner-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.get_messages(owner)
        self.get_messages(follower)
        self.prepare_follow_pair(owner, follower)

        first = self.start_follow(follower)
        ws_state = get_state()
        relationship = ws_state.get_follow_relationship("follower-1")
        with mock.patch.object(
            emo_ws,
            "createFollowSafetyLease",
            wraps=emo_ws.createFollowSafetyLease,
        ) as create_lease, mock.patch.object(
            ws_state,
            "start_follow_relationship",
            wraps=ws_state.start_follow_relationship,
        ) as start_relationship, mock.patch.object(
            ws_state,
            "subscribe_playback_context",
            wraps=ws_state.subscribe_playback_context,
        ) as subscribe, mock.patch.object(
            ws_state,
            "get_follow_relationship",
            side_effect=AssertionError("durable replay read live relationship state"),
        ) as get_relationship:
            retry = self.start_follow(follower, request_id="follow-start-2")

        create_lease.assert_not_called()
        start_relationship.assert_called_once()
        subscribe.assert_called_once_with(
            mock.ANY,
            "context-source-1",
        )
        get_relationship.assert_not_called()

        self.assertEqual([message["action"] for message in first], ["system.ack"])
        self.assertEqual([message["action"] for message in retry], ["system.ack"])
        self.assertEqual(retry[0]["payload"], first[0]["payload"])
        lease = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertIsNotNone(lease)
        self.assertEqual(first[0]["payload"], lease["startAck"])
        self.assertEqual(first[0]["payload"]["status"], "active")
        self.assertEqual(
            first[0]["payload"]["suspendedPlaybackContextId"],
            "context-suspended-1",
        )
        self.assertEqual(lease["followerConnectionEpoch"], 1)
        self.assertEqual(lease["sourceConnectionEpoch"], 1)
        self.assertEqual(lease["suspendedConnectionEpoch"], 1)
        self.assertNotEqual(
            lease["followerConnectionNonce"],
            lease["sourceConnectionNonce"],
        )
        self.assertEqual(
            get_state().get_follow_relationship("follower-1")["createdAtMs"],
            relationship["createdAtMs"],
        )

        owner.disconnect(namespace="/emo")
        self.clients.remove(owner)
        offline_replay = self.start_follow(
            follower,
            request_id="follow-start-3",
        )
        self.assertEqual(offline_replay[0]["payload"], first[0]["payload"])

        follower.emit(
            "message",
            {
                "type": "command",
                "action": "follow.stop",
                "requestId": "follow-stop-1",
                "payload": {"sourcePlaybackContextId": "context-source-1"},
            },
            namespace="/emo",
        )
        stopped = self.get_messages(follower)

        self.assertEqual([message["action"] for message in stopped], ["system.ack"])
        self.assertEqual(stopped[0]["payload"], {"action": "follow.stop"})
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))
        self.assertIsNone(
            getFollowSafetyLeaseForFollower(
                "alice",
                "follower-1",
                "device:follower-1",
            )
        )

    def test_follower_disconnect_and_reconnect_resume_frozen_lease(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-1",
            source_device_session_id="device:source-1",
        )
        first_ack = self.get_ack(
            self.start_follow(follower),
            "follow-start-1",
        )
        original = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )

        follower.disconnect(namespace="/emo")
        self.clients.remove(follower)
        grace = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(grace["phase"], "reconnectGrace")
        self.assertIn("followReconnectGraceExpiresAtMs", grace)
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))

        replacement = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.get_messages(replacement)
        resumed_ack = self.get_ack(
            self.start_follow(
                replacement,
                request_id="follow-resume-1",
            ),
            "follow-resume-1",
        )
        resumed = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        generation = get_state().get_current_physical_generation(
            "alice",
            "follower-1",
            "device:follower-1",
        )

        self.assertEqual(resumed_ack["payload"], first_ack["payload"])
        self.assertEqual(resumed["phase"], "active")
        self.assertEqual(
            resumed["followerConnectionNonce"],
            generation["connectionNonce"],
        )
        self.assertNotEqual(
            resumed["followerConnectionNonce"],
            original["followerConnectionNonce"],
        )
        self.assertEqual(resumed["startAck"], original["startAck"])
        self.assertTrue(
            get_state().get_follow_relationship("follower-1")["active"]
        )
        self.assertIn(
            generation["sid"],
            get_state().list_playback_context_subscribers(
                "context-source-1",
                user_name="alice",
            ),
        )

    def test_real_follower_replacement_enters_grace_before_resume(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-1",
            source_device_session_id="device:source-1",
        )
        self.get_ack(self.start_follow(follower), "follow-start-1")
        original = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )

        replacement = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        if not follower.is_connected(namespace="/emo"):
            self.clients.remove(follower)
        self.get_messages(replacement)
        grace = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(grace["phase"], "reconnectGrace")
        self.assertEqual(
            grace["followerConnectionNonce"],
            original["followerConnectionNonce"],
        )
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))

        self.get_ack(
            self.start_follow(
                replacement,
                request_id="follow-replacement-resume",
            ),
            "follow-replacement-resume",
        )
        resumed = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(resumed["phase"], "active")
        self.assertNotEqual(
            resumed["followerConnectionNonce"],
            original["followerConnectionNonce"],
        )

    def test_source_disconnect_recovery_clears_on_fresh_generation_fact(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-1",
            source_device_session_id="device:source-1",
        )
        self.get_ack(self.start_follow(follower), "follow-start-1")
        old_lease = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )

        source.disconnect(namespace="/emo")
        self.clients.remove(source)
        recovering = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(recovering["phase"], "active")
        self.assertIn("sourceRecoveryDeadlineAtMs", recovering)
        self.assertTrue(
            get_state().get_follow_relationship("follower-1")["active"]
        )

        replacement = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        self.get_messages(replacement)
        update_messages = self.report_strict_playback_context(
            replacement,
            "source-recovery-fact",
            "context-source-1",
            "device:source-1",
            state="stopped",
            client_seq=1,
        )
        self.assertFalse(
            any(message["action"] == "system.error" for message in update_messages)
        )
        recovered = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        new_generation = get_state().get_current_physical_generation(
            "alice",
            "source-1",
            "device:source-1",
        )
        self.assertNotIn("sourceRecoveryDeadlineAtMs", recovered)
        self.assertEqual(
            recovered["sourceConnectionNonce"],
            new_generation["connectionNonce"],
        )
        self.assertNotEqual(
            recovered["sourceConnectionNonce"],
            old_lease["sourceConnectionNonce"],
        )

    def test_cleanup_stop_ignores_disabled_capability_and_is_idempotent(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-1",
            source_device_session_id="device:source-1",
        )
        self.get_ack(self.start_follow(follower), "follow-start-1")

        cleanup_client = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        if not follower.is_connected(namespace="/emo"):
            self.clients.remove(follower)
        self.get_messages(cleanup_client)
        for request_id in ("follow-stop-cleanup-1", "follow-stop-cleanup-2"):
            cleanup_client.emit(
                "message",
                {
                    "type": "command",
                    "action": "follow.stop",
                    "requestId": request_id,
                    "payload": {
                        "sourcePlaybackContextId": "context-source-1"
                    },
                },
                namespace="/emo",
            )
            ack = self.get_ack(
                self.get_messages(cleanup_client),
                request_id,
            )
            self.assertEqual(ack["payload"], {"action": "follow.stop"})

        self.assertIsNone(
            getFollowSafetyLeaseForFollower(
                "alice",
                "follower-1",
                "device:follower-1",
            )
        )
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))

    def test_follow_feedback_is_rejected_without_changing_durable_lease(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-1",
            source_device_session_id="device:source-1",
        )
        self.get_ack(self.start_follow(follower), "follow-start-1")
        before = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )

        follower.emit(
            "message",
            {
                "type": "event",
                "action": "follow.feedback",
                "requestId": "follow-feedback-rejected",
                "payload": {},
            },
            namespace="/emo",
        )

        error = self.get_error(
            self.get_messages(follower),
            "follow-feedback-rejected",
        )
        self.assertEqual(error["payload"]["code"], "not_supported")
        self.assertEqual(
            getFollowSafetyLeaseForFollower(
                "alice",
                "follower-1",
                "device:follower-1",
            ),
            before,
        )
        self.assertTrue(
            get_state().get_follow_relationship("follower-1")["active"]
        )

    def test_follower_playback_update_cannot_mutate_suspended_context(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-1",
            source_device_session_id="device:source-1",
        )
        self.get_ack(self.start_follow(follower), "follow-start-1")
        before_context = getPlaybackContextState("context-suspended-1")
        before_device = getDevicePlaybackState(
            "context-suspended-1",
            "follower-1",
        )
        before_lease = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )

        follower.emit(
            "message",
            {
                "type": "event",
                "action": "playback.update",
                "requestId": "follow-suspended-update-rejected",
                "payload": {
                    "playbackContextId": "context-suspended-1",
                    "deviceSessionId": "device:follower-1",
                    "origin": "passive",
                    "appliedControlVersion": 1,
                    "state": "stopped",
                    "trackId": "song-follower-1",
                    "positionMs": 200,
                    "positionSampledAtServerMs": 1,
                    "playbackRate": 1.0,
                    "clientSeq": 2,
                },
            },
            namespace="/emo",
        )

        error = self.get_error(
            self.get_messages(follower),
            "follow-suspended-update-rejected",
        )
        self.assertEqual(error["payload"]["code"], "conflict")
        self.assertEqual(
            {
                field: error["payload"][field]
                for field in (
                    "currentEpoch",
                    "currentVersion",
                    "currentQueueRevision",
                    "currentControlVersion",
                )
            },
            {
                "currentEpoch": before_context["epoch"],
                "currentVersion": before_context["version"],
                "currentQueueRevision": before_context["queueRevision"],
                "currentControlVersion": before_context["controlVersion"],
            },
        )
        self.assertEqual(
            getPlaybackContextState("context-suspended-1"),
            before_context,
        )
        self.assertEqual(
            getDevicePlaybackState("context-suspended-1", "follower-1"),
            before_device,
        )
        self.assertEqual(
            getFollowSafetyLeaseForFollower(
                "alice",
                "follower-1",
                "device:follower-1",
            ),
            before_lease,
        )

    def test_deadline_sweep_retains_cleanup_required_fence(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-1",
            source_device_session_id="device:source-1",
        )
        self.get_ack(self.start_follow(follower), "follow-start-1")
        follower.disconnect(namespace="/emo")
        self.clients.remove(follower)
        grace = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )

        transitions = emo_ws._sweep_follow_safety_leases(
            grace["followReconnectGraceExpiresAtMs"]
        )

        self.assertEqual(
            transitions[0]["reason"],
            "followerReconnectExpired",
        )
        retained = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(retained["phase"], "cleanupRequired")
        self.assertEqual(db.EmoFollowSafetyLease.select().count(), 1)
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))

    def test_starting_a_different_source_conflicts_without_switching(self):
        first_owner = self.connect_device(
            "alice",
            "Alic3",
            "owner-1",
            "device:owner-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        second_owner = self.connect_device(
            "alice",
            "Alic3",
            "owner-2",
            "device:owner-2",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        for client in (first_owner, second_owner, follower):
            self.get_messages(client)
        self.prepare_context_fact(
            first_owner,
            "owner-1",
            "device:owner-1",
            "context-source-1",
        )
        self.prepare_context_fact(
            second_owner,
            "owner-2",
            "device:owner-2",
            "context-source-2",
        )
        self.prepare_context_fact(
            follower,
            "follower-1",
            "device:follower-1",
            "context-suspended-1",
        )
        self.get_messages(follower)
        self.start_follow(follower)

        conflict_messages = self.start_follow(
            follower,
            request_id="follow-start-conflict",
            playback_context_id="context-source-2",
        )

        error = self.get_error(conflict_messages, "follow-start-conflict")
        self.assertEqual(error["payload"]["code"], "conflict")
        self.assertEqual(
            error["payload"]["playbackContextId"],
            "context-suspended-1",
        )
        for field_name in (
            "currentEpoch",
            "currentVersion",
            "currentQueueRevision",
            "currentControlVersion",
        ):
            self.assertIn(field_name, error["payload"])
        self.assertEqual(
            get_state().get_follow_relationship("follower-1")[
                "sourcePlaybackContextId"
            ],
            "context-source-1",
        )

    def test_capability_role_device_and_user_gates(self):
        owner = self.connect_device(
            "alice",
            "Alic3",
            "owner-1",
            "device:owner-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        unsupported = self.connect_device(
            "alice",
            "Alic3",
            "unsupported-1",
            "device:unsupported-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "supportsFollow": False,
            },
        )
        controller = self.connect_device(
            "alice",
            "Alic3",
            "controller-1",
            "device:controller-1",
            ["controller"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        bob = self.connect_device(
            "bob",
            "B0b",
            "bob-player-1",
            "device:bob-player-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        for client in (owner, unsupported, controller, bob):
            self.get_messages(client)
        self.ensure_playback_context(
            owner,
            "context-create-source-1",
            playback_context_id="context-source-1",
            device_session_id="device:owner-1",
        )

        unsupported_error = self.get_error(
            self.start_follow(
                unsupported,
                request_id="follow-unsupported",
                device_session_id="device:unsupported-1",
            ),
            "follow-unsupported",
        )
        controller_error = self.get_error(
            self.start_follow(
                controller,
                request_id="follow-controller",
                device_session_id="device:controller-1",
            ),
            "follow-controller",
        )
        mismatched_device_error = self.get_error(
            self.start_follow(
                bob,
                request_id="follow-cross-user",
                device_session_id="device:bob-player-1",
            ),
            "follow-cross-user",
        )

        self.assertEqual(
            unsupported_error["payload"]["code"],
            "capability_required",
        )
        self.assertEqual(
            controller_error["payload"]["code"],
            "capability_required",
        )
        self.assertEqual(mismatched_device_error["payload"]["code"], "not_found")

    def test_follow_composite_capabilities_fail_closed(self):
        for capability in (
            CAPABILITY_PLAYBACK_CONTEXT_V2,
            "effectiveAtPlayback",
            "canPlay",
            "canPause",
            "canSeek",
            "supportsFollow",
        ):
            with self.subTest(capability=capability):
                client_id = "missing-%s" % capability.lower()
                device_session_id = "device:%s" % client_id
                capabilities = {
                    CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                    "effectiveAtPlayback": True,
                    "canPlay": True,
                    "canPause": True,
                    "canSeek": True,
                    "supportsFollow": True,
                }
                capabilities[capability] = False
                follower = self.connect_device(
                    "alice",
                    "Alic3",
                    client_id,
                    device_session_id,
                    ["player"],
                    capabilities=capabilities,
                )

                error = self.get_error(
                    self.start_follow(
                        follower,
                        request_id="follow-missing-%s" % capability,
                        playback_context_id="context-unavailable",
                        device_session_id=device_session_id,
                    ),
                    "follow-missing-%s" % capability,
                )

                self.assertEqual(
                    error["payload"]["code"],
                    "capability_required",
                )
                self.assertIsNone(
                    getFollowSafetyLeaseForFollower(
                        "alice",
                        client_id,
                        device_session_id,
                    )
                )

    def test_source_capability_and_self_follow_fail_without_side_effects(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-no-effective",
            "device:source-no-effective",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": False,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-no-effective",
            source_device_session_id="device:source-no-effective",
        )

        source_error = self.get_error(
            self.start_follow(follower, request_id="follow-source-capability"),
            "follow-source-capability",
        )

        self.assertEqual(
            source_error["payload"]["code"],
            "capability_required",
        )
        self.assertIsNone(
            getFollowSafetyLeaseForFollower(
                "alice",
                "follower-1",
                "device:follower-1",
            )
        )
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))

        self_follow = self.connect_device(
            "alice",
            "Alic3",
            "self-follow-1",
            "device:self-follow-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_context_fact(
            self_follow,
            "self-follow-1",
            "device:self-follow-1",
            "context-self-follow",
        )

        self_error = self.get_error(
            self.start_follow(
                self_follow,
                request_id="follow-self",
                playback_context_id="context-self-follow",
                device_session_id="device:self-follow-1",
            ),
            "follow-self",
        )

        self.assertEqual(self_error["payload"]["code"], "conflict")
        self.assertIsNone(
            getFollowSafetyLeaseForFollower(
                "alice",
                "self-follow-1",
                "device:self-follow-1",
            )
        )
        self.assertIsNone(get_state().get_follow_relationship("self-follow-1"))

    def test_follower_replacement_requires_current_nonce_scoped_fact(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-1",
            source_device_session_id="device:source-1",
        )
        old_generation = get_state().get_current_physical_generation(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        replacement = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        if not follower.is_connected(namespace="/emo"):
            self.clients.remove(follower)
        self.get_messages(replacement)
        replacement_generation = get_state().get_current_physical_generation(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertNotEqual(
            replacement_generation["connectionNonce"],
            old_generation["connectionNonce"],
        )

        error = self.get_error(
            self.start_follow(
                replacement,
                request_id="follow-replaced-follower",
            ),
            "follow-replaced-follower",
        )

        self.assertEqual(error["payload"]["code"], "conflict")
        self.assertEqual(
            error["payload"]["playbackContextId"],
            "context-suspended-1",
        )
        self.assertIsNone(
            getFollowSafetyLeaseForFollower(
                "alice",
                "follower-1",
                "device:follower-1",
            )
        )
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))

    def test_follow_start_commit_and_live_state_lock_boundaries(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-1",
            source_device_session_id="device:source-1",
        )
        ws_state = get_state()
        active = {
            "generation": 0,
            "dispatch": 0,
            "context": 0,
            "pair": 0,
            "resource": 0,
            "database": 0,
        }
        lock_entries = []
        observations = []
        real_lifecycle = emo_ws.strictPhysicalGenerationLockSet
        real_dispatch = emo_ws._ordinary_control_dispatch_barrier_set
        real_context = follow_store.strictPlaybackContextLockSet
        real_pair = follow_store.strictAuthorityPairLockSet
        real_resource = follow_store.followResourceLock
        real_transaction = follow_store._follow_transaction
        real_create = emo_ws.createFollowSafetyLease
        real_lease_create = follow_store.EmoFollowSafetyLease.create
        real_relationship = ws_state.start_follow_relationship
        real_subscribe = ws_state.subscribe_playback_context
        real_ack = emo_ws._send_ack

        def observed_lock(name, factory, *args, **kwargs):
            @contextmanager
            def observed():
                with factory(*args, **kwargs):
                    lock_entries.append(name)
                    active[name] += 1
                    try:
                        yield
                    finally:
                        active[name] -= 1

            return observed()

        def snapshot(label):
            observations.append(
                (
                    label,
                    dict(active),
                    db.db.in_transaction(),
                    bool(ws_state._lock._is_owned()),
                )
            )

        def observed_create(*args, **kwargs):
            result = real_create(*args, **kwargs)
            snapshot("store_return")
            self.assertIsNone(ws_state.get_follow_relationship("follower-1"))
            return result

        def observed_lease_create(*args, **kwargs):
            snapshot("lease_create")
            return real_lease_create(*args, **kwargs)

        def observed_relationship(*args, **kwargs):
            snapshot("relationship")
            self.assertIsNotNone(
                getFollowSafetyLeaseForFollower(
                    "alice",
                    "follower-1",
                    "device:follower-1",
                )
            )
            return real_relationship(*args, **kwargs)

        def observed_subscribe(*args, **kwargs):
            snapshot("subscribe")
            return real_subscribe(*args, **kwargs)

        def observed_ack(request_id=None, payload=None):
            if request_id == "follow-lock-boundary":
                snapshot("ack")
                self.assertIsNotNone(
                    ws_state.get_follow_relationship("follower-1")
                )
            return real_ack(request_id, payload)

        with mock.patch.object(
            emo_ws,
            "strictPhysicalGenerationLockSet",
            side_effect=lambda keys: observed_lock(
                "generation",
                real_lifecycle,
                keys,
            ),
        ), mock.patch.object(
            emo_ws,
            "_ordinary_control_dispatch_barrier_set",
            side_effect=lambda ids: observed_lock(
                "dispatch",
                real_dispatch,
                ids,
            ),
        ), mock.patch.object(
            follow_store,
            "strictPlaybackContextLockSet",
            side_effect=lambda ids: observed_lock(
                "context",
                real_context,
                ids,
            ),
        ), mock.patch.object(
            follow_store,
            "strictAuthorityPairLockSet",
            side_effect=lambda pairs: observed_lock(
                "pair",
                real_pair,
                pairs,
            ),
        ), mock.patch.object(
            follow_store,
            "followResourceLock",
            side_effect=lambda keys: observed_lock(
                "resource",
                real_resource,
                keys,
            ),
        ), mock.patch.object(
            follow_store,
            "_follow_transaction",
            side_effect=lambda: observed_lock(
                "database",
                real_transaction,
            ),
        ), mock.patch.object(
            follow_store.EmoFollowSafetyLease,
            "create",
            side_effect=observed_lease_create,
        ), mock.patch.object(
            emo_ws,
            "createFollowSafetyLease",
            side_effect=observed_create,
        ), mock.patch.object(
            ws_state,
            "start_follow_relationship",
            side_effect=observed_relationship,
        ), mock.patch.object(
            ws_state,
            "subscribe_playback_context",
            side_effect=observed_subscribe,
        ), mock.patch.object(
            emo_ws,
            "_send_ack",
            side_effect=observed_ack,
        ):
            messages = self.start_follow(
                follower,
                request_id="follow-lock-boundary",
            )

        self.assertEqual(
            [message[0] for message in observations],
            ["lease_create", "store_return", "relationship", "subscribe", "ack"],
        )
        self.assertEqual(
            lock_entries,
            ["generation", "dispatch", "context", "pair", "resource", "database"],
        )
        lease_locks = observations[0][1]
        self.assertEqual(lease_locks, dict.fromkeys(active, 1))
        self.assertTrue(observations[0][2])
        self.assertFalse(observations[0][3])
        for _label, locks, in_transaction, state_lock_owned in observations[1:4]:
            self.assertEqual(locks["generation"], 1)
            self.assertEqual(locks["dispatch"], 1)
            self.assertEqual(locks["context"], 0)
            self.assertEqual(locks["pair"], 0)
            self.assertEqual(locks["resource"], 0)
            self.assertEqual(locks["database"], 0)
            self.assertFalse(in_transaction)
            self.assertFalse(state_lock_owned)
        self.assertEqual(observations[4][1], dict.fromkeys(active, 0))
        self.assertFalse(observations[4][2])
        self.assertFalse(observations[4][3])
        self.assertEqual(
            [message["action"] for message in messages],
            ["system.ack"],
        )

    def test_follow_start_store_failure_has_no_live_side_effects(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-1",
            source_device_session_id="device:source-1",
        )
        ws_state = get_state()
        subscribers_before = ws_state.list_playback_context_subscribers(
            "context-source-1"
        )

        with mock.patch.object(
            follow_store.EmoFollowSafetyLease,
            "create",
            side_effect=RuntimeError("injected Follow lease commit failure"),
        ):
            messages = self.start_follow(
                follower,
                request_id="follow-store-failure",
            )

        error = self.get_error(messages, "follow-store-failure")
        self.assertEqual(error["payload"]["code"], "internal_error")
        self.assertFalse(
            any(message["action"] == "system.ack" for message in messages)
        )
        self.assertIsNone(
            getFollowSafetyLeaseForFollower(
                "alice",
                "follower-1",
                "device:follower-1",
            )
        )
        self.assertIsNone(ws_state.get_follow_relationship("follower-1"))
        self.assertEqual(
            ws_state.list_playback_context_subscribers("context-source-1"),
            subscribers_before,
        )

    def test_paused_and_stopped_source_facts_do_not_require_fresh_progress(self):
        for suffix, source_state in (("paused", "paused"), ("stopped", "stopped")):
            with self.subTest(source_state=source_state):
                source = self.connect_device(
                    "alice",
                    "Alic3",
                    "source-%s" % suffix,
                    "device:source-%s" % suffix,
                    ["player"],
                    capabilities={
                        CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                        "effectiveAtPlayback": True,
                        "supportsFollow": False,
                    },
                )
                follower = self.connect_device(
                    "alice",
                    "Alic3",
                    "follower-%s" % suffix,
                    "device:follower-%s" % suffix,
                    ["player"],
                    capabilities={
                        CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                        "effectiveAtPlayback": True,
                        "supportsFollow": True,
                    },
                )
                with mock.patch(
                    "supysonic.emo.ws._server_time_ms",
                    return_value=1000,
                ):
                    self.prepare_follow_pair(
                        source,
                        follower,
                        source_client_id="source-%s" % suffix,
                        source_device_session_id="device:source-%s" % suffix,
                        follower_client_id="follower-%s" % suffix,
                        follower_device_session_id="device:follower-%s" % suffix,
                        source_playback_context_id="context-source-%s" % suffix,
                        suspended_playback_context_id=(
                            "context-suspended-%s" % suffix
                        ),
                        source_state=source_state,
                        source_sampled_at_server_ms=1000,
                    )

                messages = self.start_follow(
                    follower,
                    request_id="follow-start-%s" % suffix,
                    playback_context_id="context-source-%s" % suffix,
                    device_session_id="device:follower-%s" % suffix,
                )

                ack = self.get_ack(messages, "follow-start-%s" % suffix)
                self.assertEqual(ack["payload"]["status"], "active")

    def test_stale_playing_and_idle_source_fail_without_lease(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        with mock.patch(
            "supysonic.emo.ws._server_time_ms",
            return_value=1000,
        ):
            self.prepare_follow_pair(
                source,
                follower,
                source_client_id="source-1",
                source_device_session_id="device:source-1",
                source_state="playing",
                source_sampled_at_server_ms=1000,
            )

        stale = self.get_error(
            self.start_follow(follower, request_id="follow-stale-playing"),
            "follow-stale-playing",
        )
        self.assertEqual(stale["payload"]["code"], "conflict")
        self.assertEqual(
            stale["payload"]["playbackContextId"],
            "context-source-1",
        )
        self.assertIsNone(
            getFollowSafetyLeaseForFollower(
                "alice",
                "follower-1",
                "device:follower-1",
            )
        )
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))

        idle_source = self.connect_device(
            "alice",
            "Alic3",
            "idle-source-1",
            "device:idle-source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        idle_follower = self.connect_device(
            "alice",
            "Alic3",
            "idle-follower-1",
            "device:idle-follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_context_fact(
            idle_source,
            "idle-source-1",
            "device:idle-source-1",
            "context-source-idle",
            state="idle",
            queue_song_ids=[],
            position_ms=0,
        )
        self.prepare_context_fact(
            idle_follower,
            "idle-follower-1",
            "device:idle-follower-1",
            "context-suspended-idle",
        )
        idle = self.get_error(
            self.start_follow(
                idle_follower,
                request_id="follow-idle",
                playback_context_id="context-source-idle",
                device_session_id="device:idle-follower-1",
            ),
            "follow-idle",
        )
        self.assertEqual(idle["payload"]["code"], "queue_required")
        self.assertEqual(
            idle["payload"]["playbackContextId"],
            "context-source-idle",
        )
        self.assertIsNone(
            getFollowSafetyLeaseForFollower(
                "alice",
                "idle-follower-1",
                "device:idle-follower-1",
            )
        )

    def test_source_replacement_invalidates_persisted_physical_fact(self):
        source = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.prepare_follow_pair(
            source,
            follower,
            source_client_id="source-1",
            source_device_session_id="device:source-1",
        )
        old_nonce = get_state().get_current_physical_generation(
            "alice",
            "source-1",
            "device:source-1",
        )["connectionNonce"]
        replacement = self.connect_device(
            "alice",
            "Alic3",
            "source-1",
            "device:source-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        if not source.is_connected(namespace="/emo"):
            self.clients.remove(source)
        self.get_messages(replacement)
        new_nonce = get_state().get_current_physical_generation(
            "alice",
            "source-1",
            "device:source-1",
        )["connectionNonce"]
        self.assertNotEqual(new_nonce, old_nonce)

        error = self.get_error(
            self.start_follow(follower, request_id="follow-replaced-source"),
            "follow-replaced-source",
        )

        self.assertEqual(error["payload"]["code"], "conflict")
        self.assertEqual(
            error["payload"]["playbackContextId"],
            "context-source-1",
        )
        self.assertIsNone(
            getFollowSafetyLeaseForFollower(
                "alice",
                "follower-1",
                "device:follower-1",
            )
        )

    def test_source_close_requires_follow_cleanup_and_disconnect_keeps_fence(self):
        owner = self.connect_device(
            "alice",
            "Alic3",
            "owner-1",
            "device:owner-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.get_messages(owner)
        self.get_messages(follower)
        self.prepare_follow_pair(owner, follower)
        self.start_follow(follower)
        self.get_messages(owner)

        owner.emit(
            "message",
            {
                "type": "command",
                "action": "playback.context.close",
                "requestId": "context-close-source-1",
                "payload": {
                    "playbackContextId": "context-source-1",
                    "expectedEpoch": 1,
                    "baseVersion": 1,
                },
            },
            namespace="/emo",
        )
        close_messages = self.get_messages(owner)
        follower_messages = self.get_messages(follower)

        close_ack = self.get_ack(
            close_messages,
            "context-close-source-1",
        )
        self.assertEqual(
            close_ack["payload"],
            {"action": "playback.context.close"},
        )
        self.assertTrue(
            any(
                message["action"] == "playback.context.closed"
                for message in follower_messages
            )
        )
        self.assertIsNone(
            get_state().get_follow_relationship("follower-1")
        )
        lease = getFollowSafetyLeaseForFollower(
            "alice",
            "follower-1",
            "device:follower-1",
        )
        self.assertEqual(lease["phase"], "cleanupRequired")

        follower.disconnect(namespace="/emo")
        self.clients.remove(follower)

        self.assertIsNone(get_state().get_follow_relationship("follower-1"))
        self.assertEqual(
            getFollowSafetyLeaseForFollower(
                "alice",
                "follower-1",
                "device:follower-1",
            )["phase"],
            "cleanupRequired",
        )

    def test_follow_relationship_does_not_grant_source_control(self):
        owner = self.connect_device(
            "alice",
            "Alic3",
            "owner-1",
            "device:owner-1",
            ["player"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": False,
            },
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player", "controller"],
            capabilities={
                CAPABILITY_PLAYBACK_CONTEXT_V2: True,
                "effectiveAtPlayback": True,
                "supportsFollow": True,
            },
        )
        self.get_messages(owner)
        self.get_messages(follower)
        self.prepare_follow_pair(owner, follower)
        self.get_ack(self.start_follow(follower), "follow-start-1")
        context_before = get_state().get_playback_context("context-source-1")

        follower.emit(
            "message",
            {
                "type": "command",
                "action": "player.seek",
                "requestId": "follow-source-control-1",
                "payload": {
                    "playbackContextId": "context-source-1",
                    "baseControlVersion": 1,
                    "positionMs": 9000,
                },
            },
            namespace="/emo",
        )
        error = self.get_error(
            self.get_messages(follower),
            "follow-source-control-1",
        )

        self.assertEqual(error["payload"]["code"], "forbidden")
        self.assertFalse(
            any(
                message["action"] == "player.seek"
                for message in self.get_messages(owner)
            )
        )
        context = get_state().get_playback_context("context-source-1")
        self.assertEqual(
            context["controlVersion"],
            context_before["controlVersion"],
        )
        self.assertEqual(context["positionMs"], context_before["positionMs"])


def load_tests(loader, standard_tests, pattern):
    del loader, standard_tests, pattern
    suite = unittest.TestSuite()
    for test_name in sorted(
        name
        for name in StrictV2FollowTestCase.__dict__
        if name.startswith("test_")
    ):
        suite.addTest(StrictV2FollowTestCase(test_name))
    return suite


if __name__ == "__main__":
    unittest.main()
