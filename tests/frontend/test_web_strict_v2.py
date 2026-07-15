import os
import re
import subprocess
import tempfile

from flask import current_app

from .frontendtestbase import FrontendTestBase


FORBIDDEN_STRICT_ACTIONS = (
    "player.setVolume",
    "player.requestState",
    "session.subscribe",
    "session.unsubscribe",
    "queue.local.get",
    "queue.local.set",
    "queue.session.sync",
    "queue.ready.complete",
)


class WebStrictV2FrontendTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        self._login("alice", "Alic3")

    def set_protocol(self, protocol):
        with self.app_context():
            current_app.config["WEBAPP"]["emo_web_realtime_protocol"] = protocol

    def set_optional_profiles(self, enabled):
        with self.app_context():
            current_app.config["WEBAPP"].update(
                {
                    "emo_web_strict_v2_follow_enabled": enabled,
                    "emo_web_strict_v2_handoff_enabled": enabled,
                    "emo_web_strict_v2_broadcast_enabled": enabled,
                }
            )

    def assert_inline_scripts_parse(self, html):
        scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.DOTALL)
        self.assertTrue(scripts)
        for script in scripts:
            path = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".js", encoding="utf-8", delete=False
                ) as script_file:
                    script_file.write(script)
                    path = script_file.name
                result = subprocess.run(
                    ["node", "--check", path],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            finally:
                if path:
                    os.remove(path)

    def test_legacy_mode_remains_default_and_uses_browser_credential(self):
        player = self.client.get("/player")
        control = self.client.get("/control")
        self.assertIn("'queue.local.get'", player.data)
        self.assertIn("'session.subscribe'", control.data)
        self.assertIn("authPasswordUrl", player.data)
        self.assertIn("oneTimePassword", player.data)
        self.assertIn("authPasswordUrl", control.data)
        self.assertNotIn("payload: {}", player.data[player.data.index("async function authenticateBrowserSocket"):])

    def test_strict_mode_renders_shared_client_without_legacy_action_surface(self):
        self.set_protocol("strict_v2")
        for path in ("/player", "/control"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("emo_strict_v2_client.js", response.data)
            self.assertIn("PlaybackContext strict-v2 2.2.0", response.data)
            for action in FORBIDDEN_STRICT_ACTIONS:
                self.assertNotIn(action, response.data)
            self.assertNotIn('"sessionId"', response.data)
            self.assertNotIn('"sourceSessionId"', response.data)
            self.assert_inline_scripts_parse(response.data)

    def test_strict_player_exposes_context_follow_broadcast_handoff_and_owner_lock(self):
        self.set_protocol("strict_v2")
        response = self.client.get("/player")
        for evidence in (
            "new api.PlayerOwnerLock",
            "playback.context.create",
            "queue.context.sync",
            "playback.context.close",
            "playback.update",
            "follow.start",
            "follow.stop",
            "playback.ready",
            "playback.handoff.complete",
            "const handoffCommit = message.action === 'player.play' && payload.handoffId",
            "reportFeedback(releasedContextId)",
            "safePlay('播放失败').then(() => reportFeedback())",
            "feedbackMutation: Promise.resolve()",
            "contextSnapshots: new Map()",
            "state.client.isCurrentContextSnapshot(context)",
            "const canonicalContext = state.client.cursor(context.playbackContextId)",
            "latestSnapshot.epoch !== snapshot.epoch",
            "deviceStates.find((item) => item.clientId === context.authorityClientId)",
            "loadRequiredCurrent(projected, false)",
            "META_BATCH_SIZE = 50",
            "missing.slice(offset, offset + META_BATCH_SIZE)",
            "audio.dataset.trackId === expectedTrackId",
            "loadRequiredCurrent(payload.positionMs)",
            "loadRequiredCurrent(snapshot.positionMs || 0)",
            "clearPendingPrepare('media_load_failed')",
            "await closeContext()",
            "runQueueMutation(resetDevice)",
            "state.playbackState = 'paused'",
            "broadcast.",
            "strict-v2 队列不支持重复曲目",
            "autoplay_blocked",
        ):
            self.assertIn(evidence, response.data)
        for profile in ('"broadcast": false', '"follow": false', '"handoff": false'):
            self.assertIn(profile, response.data)
        self.assertIn("stopFollow('source offline')", response.data)
        self.assertIn("broadcast.status", response.data)

    def test_strict_control_uses_context_cursor_and_disables_unsupported_operations(self):
        self.set_protocol("strict_v2")
        response = self.client.get("/control")
        for evidence in (
            "fetchContextBindings",
            "state.client.subscribe",
            "selectionGeneration",
            "generation !== state.selectionGeneration",
            "contextRenderGeneration",
            "renderGeneration !== state.contextRenderGeneration",
            "META_BATCH_SIZE = 50",
            "missing.slice(offset, offset + META_BATCH_SIZE)",
            "state.client.isCurrentContextSnapshot(receivedContext)",
            "playback.context.status",
            "baseControlVersion",
            "baseQueueRevision",
            "authority_offline",
            "capability_required",
            "broadcast.start",
            "playback.handoff.start",
            "Follow 必须由实际 follower player 发起",
            "远程音量不可用",
        ):
            self.assertIn(evidence, response.data)
        self.assertIn('"broadcast": false', response.data)

    def test_strict_control_renders_professional_workspace_and_contextless_diagnostics(self):
        self.set_protocol("strict_v2")
        response = self.client.get("/control")

        self.assertEqual(response.status_code, 200)
        for evidence in (
            'class="strict-control-workspace"',
            'class="strict-control-metrics mb-3"',
            'id="strict-player-count"',
            'id="strict-context-empty"',
            'id="strict-context-active"',
            'id="strict-now-cover"',
            'id="strict-broadcast-participants"',
            'id="strict-broadcast-tab"',
            'id="strict-handoff-tab"',
            'id="strict-follow-tab"',
            'class="card strict-control-diagnostics mt-3"',
            "设备在线，但客户端尚未创建 PlaybackContext",
            "if (!nextContextId)",
            "state.selectedClientId = nextClientId",
            "updatePlaybackProgress()",
        ):
            self.assertIn(evidence, response.data)
        self.assertNotIn("<style>", response.data)
        self.assertIn('aria-current="page"', response.data)

    def test_optional_web_profiles_require_explicit_configuration(self):
        self.set_protocol("strict_v2")
        self.set_optional_profiles(True)
        player = self.client.get("/player")
        control = self.client.get("/control")
        for profile in ('"broadcast": true', '"follow": true', '"handoff": true'):
            self.assertIn(profile, player.data)
        self.assertIn('"broadcast": true', control.data)

    def test_invalid_protocol_value_falls_back_to_explicit_legacy_mode(self):
        self.set_protocol("future")
        response = self.client.get("/player")
        self.assertIn("'queue.local.get'", response.data)
        self.assertNotIn("emo_strict_v2_client.js", response.data)


if __name__ == "__main__":
    import unittest

    unittest.main()
