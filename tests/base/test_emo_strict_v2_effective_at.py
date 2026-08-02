import unittest
from unittest import mock

from supysonic.emo.strict_v2_effective_at import (
    EffectiveAtEligibilityError,
    getTrackDurationMs,
    isClockGateReady,
    projectBroadcastPositionMs,
    requireEffectiveAtPlayer,
    validateBroadcastSourceState,
    validateFeedbackPositionMs,
)
from supysonic.emo.ws_state import WebSocketState


class StrictV2EffectiveAtTestCase(unittest.TestCase):
    def setUp(self):
        self.state = WebSocketState()
        self.state.register_session("sid-1", now=10)
        self.state.authenticate_session("sid-1", "alice")
        self.state.register_client(
            "sid-1",
            "source-1",
            {
                "userName": "alice",
                "roles": ["player"],
                "deviceSessionId": "device:source-1",
                "capabilities": {
                    "playbackContextV2": True,
                    "canPlay": True,
                    "canPause": True,
                    "canSeek": True,
                    "effectiveAtPlayback": True,
                    "supportsBroadcast": True,
                },
            },
            now=10,
        )

    def _context(self):
        return {
            "playbackContextId": "context-1",
            "userName": "alice",
            "authorityClientId": "source-1",
            "authorityDeviceSessionId": "device:source-1",
            "lifecycle": "active",
            "queueSongIds": ["song-1", "song-2"],
            "currentIndex": 0,
            "trackId": "song-1",
            "state": "playing",
            "version": 9,
            "queueRevision": 8,
            "controlVersion": 7,
            "epoch": 2,
        }

    def _device_state(self):
        return {
            "playbackContextId": "context-1",
            "sourceClientId": "source-1",
            "deviceSessionId": "device:source-1",
            "contextEpoch": 2,
            "isAuthority": True,
            "appliedControlVersion": 7,
            "clientSeq": 12,
            "state": "playing",
            "trackId": "song-1",
            "positionMs": 1000,
            "positionSampledAtServerMs": 9000,
            "serverUpdatedAtMs": 9300,
            "playbackRate": 1.0,
        }

    def test_clock_gate_requires_three_current_nonce_pings_and_15_seconds(self):
        for now in (10.1, 10.2):
            self.state.record_clock_ping("sid-1", now=now)
        with self.assertRaises(EffectiveAtEligibilityError) as conflict:
            requireEffectiveAtPlayer(
                self.state, "alice", "source-1", now_ms=10200)
        self.assertEqual(conflict.exception.reason, "clock_unsynchronized")

        self.state.record_clock_ping("sid-1", now=10.3)
        ready = requireEffectiveAtPlayer(
            self.state,
            "alice",
            "source-1",
            now_ms=25300,
        )
        self.assertEqual(ready["clockGate"]["clockPingCount"], 3)
        with self.assertRaises(EffectiveAtEligibilityError):
            requireEffectiveAtPlayer(
                self.state,
                "alice",
                "source-1",
                now_ms=25301,
            )

    def test_clock_gate_rejects_future_or_missing_samples(self):
        self.assertFalse(isClockGateReady(None, now_ms=10000))
        self.assertFalse(isClockGateReady(
            {"clockPingCount": 3, "lastClockPingAtMs": 10001},
            now_ms=10000,
        ))

    def test_source_uses_both_freshness_clocks_and_sample_projection(self):
        anchor = validateBroadcastSourceState(
            self._context(), self._device_state(), now_ms=10000)
        self.assertEqual(anchor["sourceControlVersion"], 7)
        projected = projectBroadcastPositionMs(
            anchor["positionMs"],
            anchor["positionSampledAtServerMs"],
            10300,
            anchor["playbackRate"],
        )
        self.assertEqual(projected, 2300)

        stale_sample = self._device_state()
        stale_sample["positionSampledAtServerMs"] = 7999
        with self.assertRaises(EffectiveAtEligibilityError) as conflict:
            validateBroadcastSourceState(
                self._context(), stale_sample, now_ms=10000)
        self.assertEqual(conflict.exception.reason, "source_state_stale")

        stale_receive = self._device_state()
        stale_receive["serverUpdatedAtMs"] = 7999
        with self.assertRaises(EffectiveAtEligibilityError) as conflict:
            validateBroadcastSourceState(
                self._context(), stale_receive, now_ms=10000)
        self.assertEqual(conflict.exception.reason, "source_state_stale")

    def test_source_playing_ignores_context_control_state(self):
        for context_state in ("paused", "stopped"):
            with self.subTest(context_state=context_state):
                context = self._context()
                context["state"] = context_state

                anchor = validateBroadcastSourceState(
                    context,
                    self._device_state(),
                    now_ms=10000,
                )

                self.assertEqual(anchor["state"], "playing")
                self.assertEqual(anchor["sourceControlVersion"], 7)
                self.assertEqual(anchor["trackId"], "song-1")

    def test_source_rejects_future_sample_unsettled_track_and_rate(self):
        cases = (
            ("positionSampledAtServerMs", 10051, "clock_unsynchronized"),
            ("clientSeq", 0, "source_state_unsettled"),
            ("appliedControlVersion", 6, "source_state_unsettled"),
            ("trackId", "song-2", "source_track_mismatch"),
            ("state", "paused", "source_not_playing"),
            ("state", "stopped", "source_not_playing"),
            ("playbackRate", 2.01, "rate_unsupported"),
        )
        for field, value, reason in cases:
            with self.subTest(field=field):
                device_state = self._device_state()
                device_state[field] = value
                with self.assertRaises(EffectiveAtEligibilityError) as conflict:
                    validateBroadcastSourceState(
                        self._context(), device_state, now_ms=10000)
                self.assertEqual(conflict.exception.reason, reason)

    def test_source_rejects_missing_state_and_unsettled_controls(self):
        with self.assertRaises(EffectiveAtEligibilityError) as missing:
            validateBroadcastSourceState(
                self._context(),
                None,
                now_ms=10000,
            )
        self.assertEqual(missing.exception.reason, "source_state_missing")

        with self.assertRaises(EffectiveAtEligibilityError) as unsettled:
            validateBroadcastSourceState(
                self._context(),
                self._device_state(),
                now_ms=10000,
                has_unsettled_controls=True,
            )
        self.assertEqual(
            unsettled.exception.reason,
            "source_state_unsettled",
        )

    def test_position_projection_clamps_to_known_duration(self):
        self.assertEqual(
            projectBroadcastPositionMs(9000, 1000, 3000, 1.5, 11000),
            11000,
        )
        validateFeedbackPositionMs(11000, 11000)
        with self.assertRaises(ValueError):
            validateFeedbackPositionMs(11001, 11000)

    @mock.patch("supysonic.emo.strict_v2_effective_at.close_connection")
    @mock.patch("supysonic.emo.strict_v2_effective_at.open_connection")
    @mock.patch("supysonic.emo.strict_v2_effective_at.Track.get_or_none")
    def test_track_duration_helper_returns_milliseconds(
        self,
        get_or_none,
        _open_connection,
        _close_connection,
    ):
        get_or_none.return_value = mock.Mock(duration=123)
        self.assertEqual(getTrackDurationMs("track-1"), 123000)
        get_or_none.return_value = None
        self.assertIsNone(getTrackDurationMs("track-unknown"))


if __name__ == "__main__":
    unittest.main()
