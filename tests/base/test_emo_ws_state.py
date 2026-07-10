import unittest

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "supysonic" / "emo" / "ws_state.py"
MODULE_SPEC = spec_from_file_location("emo_ws_state", MODULE_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError("Unable to load emo ws_state module")
MODULE = module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(MODULE)
WebSocketState = MODULE.WebSocketState
ClientSeqStaleError = MODULE.ClientSeqStaleError
QueueRevisionMismatchError = MODULE.QueueRevisionMismatchError
PlaybackAuthorityMismatchError = MODULE.PlaybackAuthorityMismatchError
PlaybackContextConflictError = MODULE.PlaybackContextConflictError


class EmoWebSocketStateTestCase(unittest.TestCase):
    def setUp(self):
        self.state = WebSocketState()

    def test_register_and_unregister_client(self):
        self.state.register_session("sid-1", now=100)
        self.state.authenticate_session("sid-1", "alice")
        client = self.state.register_client(
            "sid-1",
            "player-1",
            {
                "userName": "alice",
                "deviceName": "Living Room",
                "roles": ["player"],
                "sessionId": "sess-1",
            },
            now=100,
        )

        self.assertEqual(client["clientId"], "player-1")
        self.assertEqual(client["lastSeenAt"], 100)
        self.assertEqual(self.state.get_sid_for_client("player-1"), "sid-1")
        self.assertEqual(len(self.state.list_clients(user_name="alice")), 1)

        session_info, removed = self.state.unregister_session("sid-1")
        self.assertEqual(session_info["userName"], "alice")
        self.assertEqual(removed["deviceName"], "Living Room")
        self.assertIsNone(self.state.get_sid_for_client("player-1"))

    def test_touch_session_updates_client_last_seen(self):
        self.state.register_session("sid-1", now=100)
        self.state.authenticate_session("sid-1", "alice")
        self.state.register_client(
            "sid-1",
            "player-1",
            {
                "userName": "alice",
                "deviceName": "Living Room",
                "roles": ["player"],
                "sessionId": "sess-1",
            },
            now=100,
        )

        self.state.touch_session("sid-1", now=180)

        client = self.state.get_client("player-1")
        self.assertEqual(client["lastSeenAt"], 180)
        self.assertEqual(len(self.state.list_clients(stale_after_seconds=90, now=260)), 1)

    def test_list_clients_prunes_stale_client(self):
        self.state.register_session("sid-1", now=100)
        self.state.authenticate_session("sid-1", "alice")
        self.state.register_client(
            "sid-1",
            "player-1",
            {
                "userName": "alice",
                "deviceName": "Living Room",
                "roles": ["player"],
                "sessionId": "sess-1",
            },
            now=100,
        )

        clients = self.state.list_clients(stale_after_seconds=90, now=191)

        self.assertEqual(clients, [])
        self.assertIsNone(self.state.get_sid_for_client("player-1"))
        self.assertIsNone(self.state.get_client_for_sid("sid-1"))

    def test_queue_and_playback_state_are_stored_per_session(self):
        queue_state = self.state.update_queue(
            "sess-1", ["songId1", "songId2"], current_index=1, position_ms=3200
        )
        local_queue = self.state.update_local_queue(
            "sess-1", "player-1", ["songId3", "songId4"], current_index=0, position_ms=0
        )
        playback_state = self.state.update_playback_state(
            "sess-1", "player-1", {"state": "playing", "trackId": "2", "positionMs": 1234}
        )

        self.assertEqual(queue_state["currentIndex"], 1)
        self.assertEqual(queue_state["positionMs"], 3200)
        self.assertEqual(self.state.get_queue("sess-1")["queueSongIds"][0], "songId1")
        self.assertEqual(local_queue["sourceClientId"], "player-1")
        self.assertEqual(self.state.get_local_queue("sess-1", "player-1")["queueSongIds"][0], "songId3")
        self.assertEqual(playback_state["state"], "playing")
        self.assertEqual(self.state.get_playback_state("sess-1", "player-1")["trackId"], "2")
        self.assertEqual(self.state.list_playback_states("sess-1")[0]["sourceClientId"], "player-1")
        self.assertEqual(playback_state["timelineId"], "session:sess-1:client:player-1")
        self.assertEqual(playback_state["authorityClientId"], "player-1")
        self.assertEqual(playback_state["version"], 1)
        self.assertEqual(playback_state["epoch"], 1)
        self.assertIn("serverUpdatedAtMs", playback_state)

    def test_playback_client_seq_is_scoped_by_client_instance(self):
        first = self.state.update_playback_state(
            "sess-1",
            "player-1",
            {
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 1000,
                "clientInstanceId": "boot-a",
                "clientSeq": 2,
            },
            now=10,
        )

        self.assertEqual(first["serverUpdatedAtMs"], 10000)
        self.assertEqual(first["updatedAt"], 10)
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["epoch"], 1)

        with self.assertRaises(ClientSeqStaleError):
            self.state.update_playback_state(
                "sess-1",
                "player-1",
                {
                    "state": "playing",
                    "trackId": "song-1",
                    "positionMs": 1200,
                    "clientInstanceId": "boot-a",
                    "clientSeq": 2,
                },
                now=11,
            )

        restarted = self.state.update_playback_state(
            "sess-1",
            "player-1",
            {
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 1300,
                "clientInstanceId": "boot-b",
                "clientSeq": 1,
            },
            now=12,
        )

        self.assertEqual(restarted["version"], 2)
        self.assertEqual(restarted["epoch"], 1)

    def test_playback_context_authority_and_device_feedback_are_separate(self):
        context = self.state.update_playback_context_queue(
            "playback:alice:main",
            "root:phone",
            ["song-1"],
            source_client_id="phone-1",
            user_name="alice",
            now=10,
        )
        self.assertEqual(context["authorityClientId"], "phone-1")

        updated_context, authoritative = self.state.apply_authority_playback_update(
            "playback:alice:main",
            "root:phone",
            "phone-1",
            "alice",
            {"state": "playing", "trackId": "song-1", "positionMs": 100},
            now=11,
        )
        self.assertTrue(authoritative)
        self.assertEqual(updated_context["positionMs"], 100)

        pc_feedback = self.state.record_device_playback_state(
            "playback:alice:main",
            "root:pc",
            "pc-1",
            "alice",
            {"state": "playing", "trackId": "song-1", "positionMs": 999},
            is_authority=False,
            now=12,
        )
        unchanged_context, authoritative = self.state.apply_authority_playback_update(
            "playback:alice:main",
            "root:pc",
            "pc-1",
            "alice",
            {"state": "playing", "trackId": "song-1", "positionMs": 999},
            now=12,
        )

        self.assertFalse(authoritative)
        self.assertEqual(unchanged_context["authorityClientId"], "phone-1")
        self.assertEqual(unchanged_context["positionMs"], 100)
        self.assertEqual(pc_feedback["positionMs"], 999)

    def test_authority_device_volume_does_not_update_playback_context_volume(self):
        self.state.update_playback_context_queue(
            "playback:alice:main",
            "root:phone",
            ["song-1"],
            source_client_id="phone-1",
            user_name="alice",
            now=10,
        )

        updated_context, authoritative = self.state.apply_authority_playback_update(
            "playback:alice:main",
            "root:phone",
            "phone-1",
            "alice",
            {
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 100,
                "volume": 65,
            },
            now=11,
        )
        device_state = self.state.record_device_playback_state(
            "playback:alice:main",
            "root:phone",
            "phone-1",
            "alice",
            {
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 100,
                "volume": 65,
                "muted": True,
                "outputDeviceId": "dac-1",
                "audioDeviceName": "USB DAC",
            },
            is_authority=True,
            now=11,
        )

        self.assertTrue(authoritative)
        self.assertIsNone(updated_context["volume"])
        self.assertNotIn("muted", updated_context)
        self.assertNotIn("outputDeviceId", updated_context)
        self.assertNotIn("audioDeviceName", updated_context)
        self.assertEqual(device_state["volume"], 65)
        self.assertTrue(device_state["muted"])
        self.assertEqual(device_state["outputDeviceId"], "dac-1")
        self.assertEqual(device_state["audioDeviceName"], "USB DAC")

        logical_context, authoritative = self.state.apply_authority_playback_update(
            "playback:alice:main",
            "root:phone",
            "phone-1",
            "alice",
            {
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 100,
                "logicalVolume": 40,
            },
            now=12,
        )

        self.assertTrue(authoritative)
        self.assertEqual(logical_context["volume"], 40)

    def test_authority_playback_update_can_require_existing_context(self):
        context, authoritative = self.state.apply_authority_playback_update(
            "playback:alice:missing",
            "root:phone",
            "phone-1",
            "alice",
            {"state": "playing", "trackId": "song-1", "positionMs": 100},
            create_if_missing=False,
            now=11,
        )

        self.assertIsNone(context)
        self.assertFalse(authoritative)
        self.assertIsNone(self.state.get_playback_context("playback:alice:missing"))

    def test_transfer_playback_authority_keeps_playback_context_id(self):
        self.state.update_playback_context_queue(
            "playback:alice:main",
            "root:phone",
            ["song-1"],
            source_client_id="phone-1",
            user_name="alice",
            now=10,
        )

        transferred = self.state.transfer_playback_authority(
            "playback:alice:main",
            "phone-1",
            "pc-1",
            expected_control_version=1,
            playback_state={
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 200,
                "volume": 70,
            },
            origin_client_id="controller-1",
            now=11,
        )

        self.assertEqual(transferred["playbackContextId"], "playback:alice:main")
        self.assertEqual(transferred["authorityClientId"], "pc-1")
        self.assertEqual(transferred["originClientId"], "controller-1")
        self.assertEqual(transferred["controlVersion"], 2)
        self.assertEqual(transferred["positionMs"], 200)
        self.assertIsNone(transferred["volume"])

        with self.assertRaises(PlaybackAuthorityMismatchError):
            self.state.transfer_playback_authority(
                "playback:alice:main",
                "phone-1",
                "tablet-1",
            )

    def test_handoff_request_id_is_scoped_by_user(self):
        alice_handoff = self.state.create_playback_handoff(
            "handoff-alice",
            "request-1",
            "playback:alice:main",
            "alice",
            "alice-phone",
            "alice-pc",
            1,
            2,
            {},
            now=10,
        )
        bob_handoff = self.state.create_playback_handoff(
            "handoff-bob",
            "request-1",
            "playback:bob:main",
            "bob",
            "bob-phone",
            "bob-pc",
            1,
            2,
            {},
            now=11,
        )

        self.assertEqual(alice_handoff["handoffId"], "handoff-alice")
        self.assertEqual(bob_handoff["handoffId"], "handoff-bob")
        self.assertEqual(
            self.state.get_playback_handoff_by_request(
                "alice",
                "alice-phone",
                "request-1",
            )["handoffId"],
            "handoff-alice",
        )
        self.assertEqual(
            self.state.get_playback_handoff_by_request(
                "bob",
                "bob-phone",
                "request-1",
            )["handoffId"],
            "handoff-bob",
        )

    def test_handoff_request_id_is_scoped_by_origin_client(self):
        first_handoff = self.state.create_playback_handoff(
            "handoff-controller-1",
            "request-1",
            "playback:alice:main",
            "alice",
            "alice-phone",
            "alice-pc",
            1,
            2,
            {},
            origin_client_id="controller-1",
            now=10,
        )
        second_handoff = self.state.create_playback_handoff(
            "handoff-controller-2",
            "request-1",
            "playback:alice:other",
            "alice",
            "alice-tablet",
            "alice-speaker",
            1,
            2,
            {},
            origin_client_id="controller-2",
            now=11,
        )

        self.assertEqual(first_handoff["originClientId"], "controller-1")
        self.assertEqual(second_handoff["originClientId"], "controller-2")
        self.assertEqual(
            self.state.get_playback_handoff_by_request(
                "alice",
                "controller-1",
                "request-1",
            )["handoffId"],
            "handoff-controller-1",
        )
        self.assertEqual(
            self.state.get_playback_handoff_by_request(
                "alice",
                "controller-2",
                "request-1",
            )["handoffId"],
            "handoff-controller-2",
        )

    def test_queue_revision_conflict_is_independent_from_playback_version(self):
        queue = self.state.update_queue(
            "sess-1",
            ["song-1", "song-2"],
            current_index=0,
            position_ms=0,
            source_client_id="player-1",
            now=10,
        )
        self.assertEqual(queue["queueRevision"], 1)

        for seq in range(1, 4):
            self.state.update_playback_state(
                "sess-1",
                "player-1",
                {
                    "state": "playing",
                    "trackId": "song-1",
                    "positionMs": seq * 1000,
                    "clientInstanceId": "boot-a",
                    "clientSeq": seq,
                },
                now=10 + seq,
            )

        playback = self.state.get_playback_state("sess-1", "player-1")
        self.assertGreater(playback["version"], queue["version"])
        self.assertEqual(playback["queueRevision"], 1)

        updated_queue = self.state.update_queue(
            "sess-1",
            ["song-1", "song-2", "song-3"],
            current_index=0,
            position_ms=0,
            source_client_id="player-1",
            expected_queue_revision=1,
            now=20,
        )
        self.assertEqual(updated_queue["queueRevision"], 2)

        with self.assertRaises(QueueRevisionMismatchError):
            self.state.update_queue(
                "sess-1",
                ["song-1"],
                current_index=0,
                position_ms=0,
                source_client_id="player-1",
                expected_queue_revision=1,
                now=21,
            )

    def test_queue_position_update_does_not_increment_queue_revision_or_epoch(self):
        queue = self.state.update_queue(
            "sess-1",
            ["song-1", "song-2"],
            current_index=0,
            position_ms=0,
            source_client_id="player-1",
            now=10,
        )

        updated = self.state.update_queue(
            "sess-1",
            ["song-1", "song-2"],
            current_index=0,
            position_ms=1500,
            source_client_id="player-1",
            expected_queue_revision=1,
            now=11,
        )

        self.assertEqual(updated["queueRevision"], queue["queueRevision"])
        self.assertEqual(updated["epoch"], queue["epoch"])
        self.assertGreater(updated["version"], queue["version"])

    def test_restore_snapshots_preserves_timeline_versions(self):
        queue = self.state.restore_queue(
            "sess-1",
            {
                "sourceClientId": "player-1",
                "queueSongIds": ["song-1", "song-2"],
                "currentIndex": 1,
                "positionMs": 300,
                "version": 7,
                "epoch": 3,
                "queueRevision": 5,
                "controlVersion": 6,
                "serverUpdatedAtMs": 10000,
            },
        )
        playback = self.state.restore_playback_state(
            "sess-1",
            "player-1",
            {
                "state": "playing",
                "trackId": "song-2",
                "positionMs": 400,
                "version": 8,
                "epoch": 3,
                "queueRevision": 5,
                "controlVersion": 6,
                "serverUpdatedAtMs": 11000,
                "serverTimeMs": 99999,
                "clientInstanceId": "boot-a",
                "clientSeq": 4,
            },
        )
        local_queue = self.state.restore_local_queue(
            "sess-1",
            "player-1",
            {
                "queueSongIds": ["song-2"],
                "currentIndex": 0,
                "positionMs": 0,
                "serverUpdatedAtMs": 12000,
            },
        )

        self.assertEqual(queue["version"], 7)
        self.assertEqual(queue["epoch"], 3)
        self.assertEqual(queue["queueRevision"], 5)
        self.assertEqual(playback["version"], 8)
        self.assertEqual(playback["epoch"], 3)
        self.assertEqual(playback["queueRevision"], 5)
        self.assertEqual(playback["serverUpdatedAtMs"], 11000)
        self.assertNotIn("serverTimeMs", playback)
        self.assertEqual(local_queue["serverUpdatedAtMs"], 12000)

        with self.assertRaises(ClientSeqStaleError):
            self.state.update_playback_state(
                "sess-1",
                "player-1",
                {
                    "state": "playing",
                    "trackId": "song-2",
                    "positionMs": 500,
                    "clientInstanceId": "boot-a",
                    "clientSeq": 4,
                },
            )

    def test_restore_playback_state_strips_expired_effective_at(self):
        playback = self.state.restore_playback_state(
            "sess-1",
            "player-1",
            {
                "state": "playing",
                "trackId": "song-1",
                "positionMs": 0,
                "effectiveAtServerMs": 9000,
            },
            now=10,
        )

        self.assertNotIn("effectiveAtServerMs", playback)

    def test_list_followers_for_source_filters_active_relationships(self):
        self.state.start_follow_relationship(
            "laptop-1",
            "root:laptop",
            "phone-1",
            "root:phone",
            "alice",
            now=10,
        )
        self.state.start_follow_relationship(
            "tablet-1",
            "root:tablet",
            "phone-1",
            "root:phone",
            "alice",
            now=20,
        )
        self.state.start_follow_relationship(
            "speaker-1",
            "root:speaker",
            "other-1",
            "root:other",
            "alice",
            now=30,
        )
        self.state.stop_follow_relationship("tablet-1", now=40)

        followers = self.state.list_followers_for_source("phone-1")
        all_followers = self.state.list_followers_for_source("phone-1", active_only=False)

        self.assertEqual([relationship["followerClientId"] for relationship in followers], ["laptop-1"])
        self.assertEqual(
            [relationship["followerClientId"] for relationship in all_followers],
            ["laptop-1", "tablet-1"],
        )

    def test_create_prepare_supersedes_existing_prepare_for_timeline(self):
        first = self.state.create_prepare(
            "prepare-1",
            "player.play",
            "timeline-1",
            ["player-1"],
            ["player-1"],
            1,
            {"timelineId": "timeline-1"},
            1000,
            2000,
        )
        self.assertEqual(first["status"], "preparing")

        second = self.state.create_prepare(
            "prepare-2",
            "player.play",
            "timeline-1",
            ["player-1"],
            ["player-1"],
            2,
            {"timelineId": "timeline-1"},
            1100,
            2100,
        )

        self.assertEqual(second["status"], "preparing")
        self.assertEqual(self.state.get_prepare("prepare-1")["status"], "superseded")
        self.assertEqual(self.state.get_prepare("prepare-2")["status"], "preparing")

    def test_broadcast_timeline_versions_and_epoch_rules(self):
        broadcast = self.state.create_broadcast(
            "broadcast-1",
            "alice",
            "phone-1",
            ["phone-1", "pc-1"],
            ["song-1", "song-2"],
            current_index=0,
            position_ms=0,
            state_name="playing",
            updated_by_client_id="phone-1",
            now=10,
        )
        self.assertEqual(broadcast["timelineId"], "broadcast:broadcast-1")
        self.assertEqual(broadcast["version"], 1)
        self.assertEqual(broadcast["epoch"], 1)
        self.assertEqual(broadcast["queueRevision"], 1)
        self.assertEqual(broadcast["controlVersion"], 1)

        seek = self.state.update_broadcast_state(
            "broadcast-1",
            "phone-1",
            position_ms=45000,
            now=11,
        )
        self.assertEqual(seek["version"], 2)
        self.assertEqual(seek["controlVersion"], 2)
        self.assertEqual(seek["epoch"], 1)
        self.assertEqual(seek["queueRevision"], 1)

        play_item = self.state.update_broadcast_state(
            "broadcast-1",
            "phone-1",
            current_index=1,
            position_ms=0,
            state_name="playing",
            expected_version=2,
            now=12,
        )
        self.assertEqual(play_item["version"], 3)
        self.assertEqual(play_item["controlVersion"], 3)
        self.assertEqual(play_item["epoch"], 2)
        self.assertEqual(play_item["queueRevision"], 1)

        queue = self.state.update_broadcast_state(
            "broadcast-1",
            "phone-1",
            queue_song_ids=["song-3", "song-4"],
            current_index=0,
            position_ms=0,
            expected_version=3,
            increment_queue_revision=True,
            now=13,
        )
        self.assertEqual(queue["version"], 4)
        self.assertEqual(queue["controlVersion"], 4)
        self.assertEqual(queue["queueRevision"], 2)
        self.assertEqual(queue["epoch"], 3)

    def test_broadcast_playback_context_upsert_tracks_broadcast_state(self):
        context = self.state.upsert_broadcast_playback_context(
            "broadcast:alice:main",
            "broadcast-1",
            "alice",
            "server",
            "phone-1",
            ["phone-1", "pc-1"],
            ["song-1", "song-2"],
            current_index=1,
            position_ms=5000,
            state_name="playing",
            queue_revision=2,
            control_version=3,
            version=4,
            epoch=5,
            timeline_id="broadcast:broadcast-1",
            now=10,
        )

        self.assertEqual(context["contextType"], "broadcast")
        self.assertEqual(context["broadcastId"], "broadcast-1")
        self.assertEqual(context["authorityClientId"], "server")
        self.assertEqual(context["originClientId"], "phone-1")
        self.assertEqual(context["participants"], ["phone-1", "pc-1"])
        self.assertEqual(context["queueSongIds"], ["song-1", "song-2"])
        self.assertEqual(context["currentIndex"], 1)
        self.assertEqual(context["trackId"], "song-2")
        self.assertEqual(context["positionMs"], 5000)
        self.assertEqual(context["queueRevision"], 2)
        self.assertEqual(context["controlVersion"], 3)
        self.assertEqual(context["version"], 4)
        self.assertEqual(context["epoch"], 5)

        stopped = self.state.upsert_broadcast_playback_context(
            "broadcast:alice:main",
            "broadcast-1",
            "alice",
            "server",
            "phone-1",
            ["phone-1", "pc-1"],
            ["song-1", "song-2"],
            current_index=1,
            position_ms=7000,
            state_name="stopped",
            queue_revision=2,
            control_version=4,
            version=5,
            epoch=5,
            timeline_id="broadcast:broadcast-1",
            now=11,
        )

        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(stopped["positionMs"], 7000)
        self.assertEqual(stopped["controlVersion"], 4)
        self.assertEqual(stopped["version"], 5)

    def test_broadcast_playback_context_upsert_rejects_existing_normal_context(self):
        self.state.create_playback_context(
            "playback:alice:main",
            "root:phone",
            "alice",
            "phone-1",
            queue_song_ids=["song-1"],
            now=10,
        )

        with self.assertRaises(PlaybackContextConflictError):
            self.state.upsert_broadcast_playback_context(
                "playback:alice:main",
                "broadcast-1",
                "alice",
                "server",
                "phone-1",
                ["phone-1"],
                ["song-1"],
                now=11,
            )

    def test_restore_broadcast_playback_context_rebuilds_active_mappings(self):
        broadcast = self.state.restore_broadcast_playback_context(
            {
                "playbackContextId": "broadcast:alice:main",
                "contextType": "broadcast",
                "broadcastId": "broadcast-1",
                "userName": "alice",
                "ownerClientId": "phone-1",
                "authorityClientId": "server",
                "originClientId": "phone-1",
                "participants": ["phone-1", "pc-1"],
                "queueSongIds": ["song-1"],
                "currentIndex": 0,
                "trackId": "song-1",
                "positionMs": 500,
                "state": "playing",
                "queueRevision": 2,
                "controlVersion": 3,
                "version": 4,
                "epoch": 5,
                "serverUpdatedAtMs": 10000,
            }
        )

        self.assertEqual(broadcast["broadcastId"], "broadcast-1")
        self.assertEqual(broadcast["ownerClientId"], "phone-1")
        self.assertTrue(self.state.is_broadcast_participant("broadcast-1", "pc-1"))
        self.assertEqual(
            self.state.get_active_broadcast_for_client("phone-1"),
            "broadcast-1",
        )

    def test_create_prepare_rejects_duplicate_broadcast_context_reservation(self):
        self.state.create_prepare(
            "prepare-1",
            "broadcast.start",
            "broadcast:broadcast-1",
            ["phone-1"],
            ["phone-1"],
            1,
            {"playbackContextId": "broadcast:alice:main"},
            1000,
            2000,
        )

        with self.assertRaises(PlaybackContextConflictError):
            self.state.create_prepare(
                "prepare-2",
                "broadcast.start",
                "broadcast:broadcast-2",
                ["phone-1"],
                ["phone-1"],
                1,
                {"playbackContextId": "broadcast:alice:main"},
                1100,
                2100,
            )

    def test_re_registering_same_client_id_keeps_latest_session_mapping(self):
        self.state.register_session("sid-1")
        self.state.authenticate_session("sid-1", "root")
        self.state.register_client(
            "sid-1",
            "controller-1",
            {
                "userName": "root",
                "deviceName": "Phone Remote",
                "roles": ["controller"],
                "sessionId": "root:living-room",
            },
        )

        self.state.register_session("sid-2")
        self.state.authenticate_session("sid-2", "root")
        self.state.register_client(
            "sid-2",
            "controller-1",
            {
                "userName": "root",
                "deviceName": "Phone Remote",
                "roles": ["controller"],
                "sessionId": "root:living-room",
            },
        )

        session_info, removed = self.state.unregister_session("sid-1")
        self.assertEqual(session_info["userName"], "root")
        self.assertIsNone(removed)
        self.assertEqual(self.state.get_sid_for_client("controller-1"), "sid-2")
        self.assertIsNotNone(self.state.get_client_for_sid("sid-2"))

    def test_session_subscriptions_are_tracked_and_cleared(self):
        self.state.register_session("sid-1")
        self.state.authenticate_session("sid-1", "root")
        self.state.subscribe_session("sid-1", "root:living-room")
        self.state.subscribe_session("sid-1", "root:bedroom")

        subscribers = self.state.list_subscribers("root:living-room", user_name="root")
        self.assertEqual(subscribers, ["sid-1"])

        remaining = self.state.unsubscribe_session("sid-1", "root:living-room")
        self.assertEqual(remaining, ["root:bedroom"])
        self.assertEqual(self.state.list_subscribers("root:living-room", user_name="root"), [])

        self.state.unregister_session("sid-1")
        self.assertEqual(self.state.list_subscribers("root:bedroom", user_name="root"), [])

    def test_playback_context_subscriptions_are_tracked_and_cleared(self):
        self.state.register_session("sid-1")
        self.state.authenticate_session("sid-1", "root")
        self.state.subscribe_playback_context("sid-1", "playback:root:main")
        self.state.subscribe_playback_context("sid-1", "playback:root:bedroom")

        subscribers = self.state.list_playback_context_subscribers(
            "playback:root:main",
            user_name="root",
        )
        self.assertEqual(subscribers, ["sid-1"])

        remaining = self.state.unsubscribe_playback_context("sid-1", "playback:root:main")
        self.assertEqual(remaining, ["playback:root:bedroom"])
        self.assertEqual(
            self.state.list_playback_context_subscribers(
                "playback:root:main",
                user_name="root",
            ),
            [],
        )

        self.state.unregister_session("sid-1")
        self.assertEqual(
            self.state.list_playback_context_subscribers(
                "playback:root:bedroom",
                user_name="root",
            ),
            [],
        )

    def test_close_playback_context_updates_state_and_clears_subscriptions(self):
        context, created = self.state.create_playback_context(
            "playback:root:main",
            "root:phone",
            "root",
            "phone-1",
            queue_song_ids=["song-1"],
            current_index=0,
            position_ms=1000,
            now=10,
        )
        self.assertTrue(created)
        self.assertEqual(context["state"], "stopped")

        self.state.register_session("sid-1")
        self.state.authenticate_session("sid-1", "root")
        self.state.subscribe_playback_context("sid-1", "playback:root:main")

        closed = self.state.close_playback_context(
            "playback:root:main",
            updated_by_client_id="phone-1",
            now=11,
        )
        cleared = self.state.clear_playback_context_subscriptions("playback:root:main")

        self.assertEqual(closed["state"], "closed")
        self.assertEqual(closed["originClientId"], "phone-1")
        self.assertEqual(closed["controlVersion"], 1)
        self.assertEqual(closed["version"], 1)
        self.assertEqual(cleared, 1)
        self.assertEqual(
            self.state.list_playback_context_subscribers(
                "playback:root:main",
                user_name="root",
            ),
            [],
        )
