import unittest
from pathlib import Path

from supysonic.db import User
from supysonic.emo.ws_state import get_state

from .frontendtestbase import FrontendTestBase


ROOT = Path(__file__).resolve().parents[2]


def read_project_file(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def clear_emo_state():
    state = get_state()
    state._sessions.clear()
    state._client_to_sid.clear()
    state._clients.clear()
    state._queues.clear()
    state._local_queues.clear()
    state._playback_states.clear()
    state._playback_timelines.clear()
    state._session_subscriptions.clear()
    state._broadcasts.clear()
    state._broadcast_participants.clear()
    state._broadcast_playback_states.clear()
    state._client_active_broadcast.clear()
    state._follow_relationships.clear()


class DeviceAliasDisplayTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        clear_emo_state()
        alice = User.get(User.name == "alice")
        with self.client.session_transaction() as session:
            session["userid"] = str(alice.id)

    def tearDown(self):
        clear_emo_state()
        super().tearDown()

    def test_devices_page_prefers_alias_for_visible_device_name(self):
        get_state().register_client(
            "sid-player-1",
            "player-1",
            {
                "userName": "alice",
                "deviceName": "Windows Player",
                "alias": "\u5ba2\u5385\u64ad\u653e\u5668",
                "roles": ["player"],
                "sessionId": "sess-main",
                "capabilities": {},
            },
        )

        response = self.client.get("/devices")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            '<div class="console-table-title">\u5ba2\u5385\u64ad\u653e\u5668</div>',
            response.data,
        )

    def test_control_template_uses_alias_display_helper(self):
        template = read_project_file("supysonic", "templates", "control.html")

        self.assertIn("function getDeviceDisplayName(device)", template)
        self.assertIn("return alias || deviceName || clientId || '-';", template)
        self.assertIn(
            "target ? getDeviceDisplayName(target) : '-'",
            template,
        )
        self.assertIn(
            "const displayName = getDeviceDisplayName(device);",
            template,
        )

    def test_control_template_exposes_follow_playback_panel(self):
        template = read_project_file("supysonic", "templates", "control.html")

        self.assertIn('id="control-follow-panel"', template)
        self.assertIn("function startFollowPlayback()", template)
        self.assertIn("function renderFollowPanel()", template)
        self.assertIn("action: 'follow.start'", template)
        self.assertIn("sourceClientId: target.clientId", template)
        self.assertIn("action: 'follow.stop'", template)
        self.assertIn("payload.sourceClientId === controlState.follow.followSourceClientId", template)
        self.assertIn("clientId: controlState.selectedClientId", template)
        self.assertIn("pendingFollow", template)
        self.assertIn("message.requestId === controlState.pendingFollow.requestId", template)
        self.assertIn("sendSessionAction('session.unsubscribe', offlineFollowSessionId)", template)

    def test_control_template_exposes_broadcast_playback_panel(self):
        template = read_project_file("supysonic", "templates", "control.html")

        self.assertIn('id="control-broadcast-panel"', template)
        self.assertIn("roles: ['controller']", template)
        self.assertIn("function startBroadcastPlayback(targetMode)", template)
        self.assertIn("function renderBroadcastPanel()", template)
        self.assertIn("function sendBroadcastQueueSync()", template)
        self.assertIn("function sendBroadcastPlayItem(index)", template)
        self.assertIn("sendBroadcastMessage('broadcast.start', 'command', payload)", template)
        self.assertIn("startBroadcastPlayback('allOnlinePlayers')", template)
        self.assertIn("startBroadcastPlayback('selectedClients')", template)
        self.assertIn("baseControlVersion: controlState.broadcast.broadcast?.controlVersion", template)
        self.assertIn("baseVersion: controlState.broadcast.broadcast?.version", template)
        self.assertIn("baseQueueRevision: currentQueue?.queueRevision", template)
        self.assertIn("sendBroadcastTransport('broadcast.seek', { positionMs })", template)

    def test_control_template_session_queue_sync_uses_selected_player_owner(self):
        template = read_project_file("supysonic", "templates", "control.html")
        start = template.index("function sendQueueSync()")
        end = template.index("function sendLocalQueue()", start)
        body = template[start:end]

        self.assertIn(
            "!controlState.selectedSessionId || !controlState.selectedClientId",
            body,
        )
        self.assertIn("clientId: controlState.selectedClientId", body)
        self.assertIn("baseQueueRevision: currentQueue?.queueRevision", body)

    def test_devices_template_uses_alias_display_helper_for_refreshes(self):
        template = read_project_file("supysonic", "templates", "devices.html")

        self.assertIn("function getDeviceDisplayName(device)", template)
        self.assertIn("return alias || deviceName || clientId || \"-\";", template)
        self.assertIn(
            "const displayName = getDeviceDisplayName(device);",
            template,
        )


if __name__ == "__main__":
    unittest.main()
