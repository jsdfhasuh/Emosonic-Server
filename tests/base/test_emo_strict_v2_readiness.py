import unittest

from supysonic.emo.strict_v2_readiness import (
    CoreProfileNotReady,
    get_effective_profile_readiness,
    negotiate_capabilities,
)


class StrictV2ReadinessTestCase(unittest.TestCase):
    def setUp(self):
        self.capabilities = {
            "playbackContextV2": True,
            "playbackPrepare": True,
            "effectiveAtPlayback": True,
            "canPlay": True,
            "canPause": True,
            "canSeek": True,
            "canSetVolume": True,
            "supportsFollow": True,
            "supportsBroadcast": True,
        }
        self.code_ready = {
            "core": True,
            "follow": True,
            "handoff": True,
            "broadcast": True,
        }
        self.deployment_enabled = {
            "emo_strict_v2_core_enabled": True,
            "emo_strict_v2_follow_enabled": True,
            "emo_strict_v2_handoff_enabled": True,
            "emo_strict_v2_broadcast_enabled": True,
        }

    def test_code_and_deployment_are_both_required(self):
        code_disabled = dict(self.code_ready, follow=False)
        deployment_disabled = dict(
            self.deployment_enabled,
            emo_strict_v2_handoff_enabled=False,
        )

        self.assertFalse(
            get_effective_profile_readiness(
                self.deployment_enabled,
                code_disabled,
            )["follow"]
        )
        self.assertFalse(
            get_effective_profile_readiness(
                deployment_disabled,
                self.code_ready,
            )["handoff"]
        )

    def test_core_not_ready_fails_closed(self):
        with self.assertRaises(CoreProfileNotReady):
            negotiate_capabilities(
                self.capabilities,
                ["player"],
                self.deployment_enabled,
                dict(self.code_ready, core=False),
            )

    def test_optional_profiles_negotiate_independently(self):
        deployment = dict(
            self.deployment_enabled,
            emo_strict_v2_follow_enabled=False,
        )

        negotiated = negotiate_capabilities(
            self.capabilities,
            ["player", "controller"],
            deployment,
            self.code_ready,
        )

        self.assertFalse(negotiated["supportsFollow"])
        self.assertTrue(negotiated["playbackPrepare"])
        self.assertTrue(negotiated["effectiveAtPlayback"])
        self.assertTrue(negotiated["supportsBroadcast"])

    def test_player_dependencies_gate_follow_and_handoff(self):
        negotiated = negotiate_capabilities(
            self.capabilities,
            ["controller"],
            self.deployment_enabled,
            self.code_ready,
        )

        self.assertFalse(negotiated["supportsFollow"])
        self.assertFalse(negotiated["playbackPrepare"])
        self.assertFalse(negotiated["effectiveAtPlayback"])
        self.assertTrue(negotiated["supportsBroadcast"])

    def test_player_without_can_play_cannot_negotiate_follow_or_handoff(self):
        capabilities = dict(self.capabilities, canPlay=False)

        negotiated = negotiate_capabilities(
            capabilities,
            ["player"],
            self.deployment_enabled,
            self.code_ready,
        )

        self.assertFalse(negotiated["supportsFollow"])
        self.assertFalse(negotiated["playbackPrepare"])
        self.assertFalse(negotiated["effectiveAtPlayback"])


if __name__ == "__main__":
    unittest.main()
