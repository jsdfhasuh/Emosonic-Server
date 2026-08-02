import unittest

from supysonic.emo.ws_state import get_state

from tests.base.test_emo_ws import (
    CAPABILITY_PLAYBACK_CONTEXT_V2,
    EmoWebSocketTestCase,
)


class StrictV2FollowTestCase(EmoWebSocketTestCase):
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
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.get_messages(owner)
        self.get_messages(follower)
        self.ensure_playback_context(
            owner,
            "context-create-source-1",
            playback_context_id="context-source-1",
            device_session_id="device:owner-1",
        )
        self.get_messages(follower)

        first = self.start_follow(follower)
        relationship = get_state().get_follow_relationship("follower-1")
        retry = self.start_follow(follower, request_id="follow-start-2")

        self.assertEqual([message["action"] for message in first], ["system.ack"])
        self.assertEqual(first[0]["payload"], {"action": "follow.start"})
        self.assertEqual([message["action"] for message in retry], ["system.ack"])
        self.assertEqual(
            get_state().get_follow_relationship("follower-1")["createdAtMs"],
            relationship["createdAtMs"],
        )

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

    def test_starting_a_different_source_conflicts_without_switching(self):
        first_owner = self.connect_device(
            "alice",
            "Alic3",
            "owner-1",
            "device:owner-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        second_owner = self.connect_device(
            "alice",
            "Alic3",
            "owner-2",
            "device:owner-2",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        for client in (first_owner, second_owner, follower):
            self.get_messages(client)
        self.ensure_playback_context(
            first_owner,
            "context-create-source-1",
            playback_context_id="context-source-1",
            device_session_id="device:owner-1",
        )
        self.ensure_playback_context(
            second_owner,
            "context-create-source-2",
            playback_context_id="context-source-2",
            device_session_id="device:owner-2",
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
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
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
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        bob = self.connect_device(
            "bob",
            "B0b",
            "bob-player-1",
            "device:bob-player-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
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
        self.assertEqual(mismatched_device_error["payload"]["code"], "forbidden")

    def test_disconnect_and_context_close_clear_relationship(self):
        owner = self.connect_device(
            "alice",
            "Alic3",
            "owner-1",
            "device:owner-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.get_messages(owner)
        self.get_messages(follower)
        self.ensure_playback_context(
            owner,
            "context-create-source-1",
            playback_context_id="context-source-1",
            device_session_id="device:owner-1",
        )
        self.start_follow(follower)
        self.get_messages(owner)

        owner.emit(
            "message",
            {
                "type": "command",
                "action": "playback.context.close",
                "requestId": "context-close-source-1",
                "payload": {"playbackContextId": "context-source-1"},
            },
            namespace="/emo",
        )
        self.get_messages(owner)
        follower_messages = self.get_messages(follower)

        self.assertTrue(
            any(
                message["action"] == "playback.context.closed"
                for message in follower_messages
            )
        )
        self.assertIsNone(get_state().get_follow_relationship("follower-1"))

        second_context_owner = self.connect_device(
            "alice",
            "Alic3",
            "owner-2",
            "device:owner-2",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.get_messages(second_context_owner)
        self.ensure_playback_context(
            second_context_owner,
            "context-create-source-2",
            playback_context_id="context-source-2",
            device_session_id="device:owner-2",
        )
        self.start_follow(
            follower,
            request_id="follow-start-source-2",
            playback_context_id="context-source-2",
        )
        follower.disconnect(namespace="/emo")
        self.clients.remove(follower)

        self.assertIsNone(get_state().get_follow_relationship("follower-1"))

    def test_follow_relationship_does_not_grant_source_control(self):
        owner = self.connect_device(
            "alice",
            "Alic3",
            "owner-1",
            "device:owner-1",
            ["player"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        follower = self.connect_device(
            "alice",
            "Alic3",
            "follower-1",
            "device:follower-1",
            ["player", "controller"],
            capabilities={CAPABILITY_PLAYBACK_CONTEXT_V2: True},
        )
        self.get_messages(owner)
        self.get_messages(follower)
        self.ensure_playback_context(
            owner,
            "context-create-source-1",
            playback_context_id="context-source-1",
            device_session_id="device:owner-1",
        )
        self.get_messages(owner)
        self.get_messages(follower)
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
