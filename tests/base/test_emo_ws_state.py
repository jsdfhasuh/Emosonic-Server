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
