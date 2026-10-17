import unittest
from unittest import mock

from supysonic.emo.strict_v2_readiness import (
    CoreProfileNotReady,
    get_effective_profile_readiness,
    is_local_test_evidence_allowed,
    is_local_test_evidence_requested,
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
            "remoteVolumeControl": True,
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

    def test_explicit_empty_code_readiness_fails_closed(self):
        readiness = get_effective_profile_readiness(
            self.deployment_enabled,
            {},
        )

        self.assertEqual(
            readiness,
            {"core": False, "follow": False, "handoff": False, "broadcast": False},
        )
        with self.assertRaises(CoreProfileNotReady):
            negotiate_capabilities(
                self.capabilities,
                ["player"],
                self.deployment_enabled,
                {},
            )

    def test_local_test_evidence_requires_explicit_development_gate(self):
        self.assertFalse(is_local_test_evidence_requested({}))
        self.assertFalse(
            is_local_test_evidence_requested(
                {"emo_strict_v2_allow_local_test_evidence": "off"}
            )
        )
        self.assertTrue(
            is_local_test_evidence_requested(
                {"emo_strict_v2_allow_local_test_evidence": "on"}
            )
        )
        self.assertFalse(is_local_test_evidence_allowed({}))
        self.assertFalse(
            is_local_test_evidence_allowed(
                {"emo_strict_v2_allow_local_test_evidence": True}
            )
        )
        self.assertFalse(
            is_local_test_evidence_allowed(
                {"emo_development_mode": True}
            )
        )
        self.assertTrue(
            is_local_test_evidence_allowed(
                {
                    "emo_development_mode": "on",
                    "emo_strict_v2_allow_local_test_evidence": "yes",
                }
            )
        )
        self.assertTrue(is_local_test_evidence_allowed({}, app_testing=True))

    def test_explicit_local_test_gate_can_enable_local_runtime_readiness(self):
        deployment = dict(
            self.deployment_enabled,
            emo_development_mode=True,
            emo_strict_v2_allow_local_test_evidence=True,
        )

        with mock.patch(
            "supysonic.emo.strict_v2_readiness.get_code_conformance_readiness",
            return_value=self.code_ready,
        ) as readiness:
            self.assertEqual(
                get_effective_profile_readiness(deployment),
                self.code_ready,
            )

            negotiated = negotiate_capabilities(
                self.capabilities,
                ["player", "controller"],
                deployment,
            )

        self.assertEqual(readiness.call_count, 2)
        self.assertTrue(negotiated["playbackContextV2"])
        self.assertTrue(negotiated["supportsFollow"])
        self.assertTrue(negotiated["playbackPrepare"])
        self.assertTrue(negotiated["effectiveAtPlayback"])
        self.assertTrue(negotiated["supportsBroadcast"])

    def test_packaged_conformance_manifest_gates_production_readiness(self):
        self.assertEqual(
            get_effective_profile_readiness(self.deployment_enabled),
            {"core": False, "follow": False, "handoff": False, "broadcast": False},
        )
        with self.assertRaises(CoreProfileNotReady):
            negotiate_capabilities(
                self.capabilities,
                ["player"],
                self.deployment_enabled,
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

    def test_player_without_can_play_keeps_prepare_but_not_composites(self):
        capabilities = dict(self.capabilities, canPlay=False)

        negotiated = negotiate_capabilities(
            capabilities,
            ["player"],
            self.deployment_enabled,
            self.code_ready,
        )

        self.assertFalse(negotiated["supportsFollow"])
        self.assertTrue(negotiated["playbackPrepare"])
        self.assertTrue(negotiated["effectiveAtPlayback"])
        self.assertFalse(negotiated["supportsBroadcast"])

    def test_effective_at_is_independent_from_playback_prepare(self):
        capabilities = dict(
            self.capabilities,
            playbackPrepare=False,
            effectiveAtPlayback=True,
        )

        negotiated = negotiate_capabilities(
            capabilities,
            ["player"],
            self.deployment_enabled,
            self.code_ready,
        )

        self.assertFalse(negotiated["playbackPrepare"])
        self.assertTrue(negotiated["effectiveAtPlayback"])

        capabilities = dict(
            self.capabilities,
            effectiveAtPlayback=False,
            playbackPrepare=True,
        )
        negotiated = negotiate_capabilities(
            capabilities,
            ["player"],
            self.deployment_enabled,
            self.code_ready,
        )

        self.assertTrue(negotiated["playbackPrepare"])
        self.assertFalse(negotiated["effectiveAtPlayback"])

    def test_effective_at_uses_follow_only_readiness(self):
        code_ready = dict(
            self.code_ready,
            handoff=False,
            broadcast=False,
        )

        negotiated = negotiate_capabilities(
            self.capabilities,
            ["player"],
            self.deployment_enabled,
            code_ready,
        )

        self.assertTrue(negotiated["effectiveAtPlayback"])
        self.assertTrue(negotiated["supportsFollow"])
        self.assertFalse(negotiated["playbackPrepare"])
        self.assertFalse(negotiated["supportsBroadcast"])

    def test_effective_at_is_false_without_optional_profile_readiness(self):
        code_ready = dict(
            self.code_ready,
            follow=False,
            handoff=False,
            broadcast=False,
        )

        negotiated = negotiate_capabilities(
            self.capabilities,
            ["player"],
            self.deployment_enabled,
            code_ready,
        )

        self.assertFalse(negotiated["effectiveAtPlayback"])
        self.assertFalse(negotiated["supportsFollow"])
        self.assertFalse(negotiated["playbackPrepare"])
        self.assertFalse(negotiated["supportsBroadcast"])

    def test_playback_prepare_does_not_require_can_play(self):
        capabilities = dict(self.capabilities, canPlay=False)

        negotiated = negotiate_capabilities(
            capabilities,
            ["player"],
            self.deployment_enabled,
            self.code_ready,
        )

        self.assertTrue(negotiated["playbackPrepare"])

    def test_follow_requires_each_static_dependency(self):
        cases = (
            ("supportsFollow request", {"supportsFollow": False}, self.code_ready),
            ("player role", {}, self.code_ready),
            ("effectiveAtPlayback", {"effectiveAtPlayback": False}, self.code_ready),
            ("canPlay", {"canPlay": False}, self.code_ready),
            ("canPause", {"canPause": False}, self.code_ready),
            ("canSeek", {"canSeek": False}, self.code_ready),
            (
                "Follow readiness",
                {},
                dict(self.code_ready, follow=False),
            ),
        )

        for label, capability_changes, code_ready in cases:
            capabilities = dict(self.capabilities, **capability_changes)
            roles = [] if label == "player role" else ["player"]
            negotiated = negotiate_capabilities(
                capabilities,
                roles,
                self.deployment_enabled,
                code_ready,
            )
            with self.subTest(dependency=label):
                self.assertFalse(negotiated["supportsFollow"])

    def test_direct_negotiation_rejects_false_playback_context_capability(self):
        capabilities = dict(self.capabilities, playbackContextV2=False)

        with self.assertRaises(ValueError):
            negotiate_capabilities(
                capabilities,
                ["player"],
                self.deployment_enabled,
                self.code_ready,
            )

    def test_broadcast_controller_and_player_composites(self):
        controller_only = dict(
            self.capabilities,
            playbackContextV2=False,
            effectiveAtPlayback=False,
            canPlay=False,
            canPause=False,
            canSeek=False,
        )
        with self.assertRaises(ValueError):
            negotiate_capabilities(
                controller_only,
                ["controller"],
                self.deployment_enabled,
                self.code_ready,
            )

        controller_only["playbackContextV2"] = True
        negotiated = negotiate_capabilities(
            controller_only,
            ["controller"],
            self.deployment_enabled,
            self.code_ready,
        )
        self.assertTrue(negotiated["supportsBroadcast"])

        for field_name in (
            "effectiveAtPlayback",
            "canPlay",
            "canPause",
            "canSeek",
        ):
            capabilities = dict(self.capabilities, **{field_name: False})
            negotiated = negotiate_capabilities(
                capabilities,
                ["player"],
                self.deployment_enabled,
                self.code_ready,
            )
            with self.subTest(field_name=field_name):
                self.assertFalse(negotiated["supportsBroadcast"])

        negotiated = negotiate_capabilities(
            self.capabilities,
            ["player"],
            self.deployment_enabled,
            self.code_ready,
        )
        self.assertTrue(negotiated["supportsBroadcast"])

    def test_negotiated_capabilities_are_exactly_ten_booleans(self):
        negotiated = negotiate_capabilities(
            self.capabilities,
            ["player", "controller"],
            self.deployment_enabled,
            self.code_ready,
        )

        self.assertEqual(set(negotiated), set(self.capabilities))
        self.assertTrue(all(isinstance(value, bool) for value in negotiated.values()))

    def test_fixed_remote_volume_capability_is_role_gated(self):
        player = negotiate_capabilities(
            self.capabilities,
            ["player"],
            self.deployment_enabled,
            self.code_ready,
        )
        controller = negotiate_capabilities(
            dict(self.capabilities, canSetVolume=False),
            ["controller"],
            self.deployment_enabled,
            self.code_ready,
        )
        incapable_player = negotiate_capabilities(
            dict(self.capabilities, canSetVolume=False),
            ["player"],
            self.deployment_enabled,
            self.code_ready,
        )

        self.assertTrue(player["remoteVolumeControl"])
        self.assertTrue(controller["remoteVolumeControl"])
        self.assertFalse(incapable_player["remoteVolumeControl"])


if __name__ == "__main__":
    unittest.main()
