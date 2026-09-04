import unittest
from contextlib import contextmanager
from unittest import mock

from peewee import OperationalError

from supysonic.emo import ws as emo_ws


class EmoControlWatchdogTestCase(unittest.TestCase):
    def test_sweep_round_shares_one_connection_and_keeps_non_db_failures_local(self):
        events = []

        @contextmanager
        def connection_scope(*, reuse=False):
            events.append(("connection.enter", reuse))
            try:
                yield
            finally:
                events.append(("connection.exit", reuse))

        def record(name, result=None, error=None):
            def callback():
                events.append(name)
                if error is not None:
                    raise error
                return result

            return callback

        with mock.patch.object(
            emo_ws,
            "connection_scope",
            side_effect=connection_scope,
        ), mock.patch.object(
            emo_ws,
            "_sweep_permanent_device_decommissions",
            side_effect=record("decommissions", error=RuntimeError("boom")),
        ), mock.patch.object(
            emo_ws,
            "_sweep_expired_control_transactions",
            side_effect=record("controls"),
        ), mock.patch.object(
            emo_ws,
            "sweepBroadcastFeedbackDeadlines",
            side_effect=record("feedback"),
        ), mock.patch.object(
            emo_ws,
            "sweepBroadcastAuthorityDisconnectDeadlines",
            side_effect=record("authority", result=[{"broadcastId": "b-1"}]),
        ), mock.patch.object(
            emo_ws,
            "_emit_r18_broadcast_projection",
            side_effect=lambda value: events.append(
                ("projection", value["broadcastId"])
            ),
        ), mock.patch.object(
            emo_ws,
            "_sweep_follow_safety_leases",
            side_effect=record("follow"),
        ), mock.patch.object(
            emo_ws,
            "compactExpiredBroadcastStates",
            side_effect=record("compaction"),
        ), mock.patch.object(emo_ws.logger, "exception") as log_exception:
            emo_ws._run_control_watchdog_sweep_round()

        self.assertEqual(
            events,
            [
                ("connection.enter", True),
                "decommissions",
                "controls",
                "feedback",
                "authority",
                ("projection", "b-1"),
                "follow",
                "compaction",
                ("connection.exit", True),
            ],
        )
        log_exception.assert_called_once_with(
            "Strict permanent device decommission sweep failed"
        )

    def test_connection_exhaustion_aborts_the_remaining_sweep_steps(self):
        connection_error = OperationalError(1040, "Too many connections")
        with mock.patch.object(
            emo_ws,
            "connection_scope",
            return_value=mock.MagicMock(
                __enter__=mock.Mock(return_value=None),
                __exit__=mock.Mock(return_value=False),
            ),
        ), mock.patch.object(
            emo_ws,
            "_sweep_permanent_device_decommissions",
            side_effect=connection_error,
        ), mock.patch.object(
            emo_ws,
            "_sweep_expired_control_transactions",
        ) as controls, mock.patch.object(emo_ws.logger, "exception") as log_exception:
            with self.assertRaises(OperationalError):
                emo_ws._run_control_watchdog_sweep_round()

        controls.assert_not_called()
        log_exception.assert_not_called()

    def test_connection_exhaustion_backoff_caps_and_resets_after_success(self):
        failures = [
            OperationalError(1040, "Too many connections") for _ in range(6)
        ]
        outcomes = [*failures, None, OperationalError(1040, "Too many connections")]

        with mock.patch.object(
            emo_ws,
            "_run_control_watchdog_sweep_round",
            side_effect=outcomes,
        ) as run_round, mock.patch.object(
            emo_ws,
            "_control_watchdog_is_active",
            side_effect=lambda *_args: run_round.call_count < len(outcomes),
        ), mock.patch.object(emo_ws.socketio, "sleep") as sleep, mock.patch.object(
            emo_ws.logger,
            "warning",
        ) as warning:
            emo_ws._control_watchdog_sweep_later(7)

        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1, 1, 2, 4, 8, 16, 30, 1],
        )
        self.assertEqual(
            [call.args[1] for call in warning.call_args_list],
            [1, 2, 4, 8, 16, 30, 1],
        )

    def test_connection_exhaustion_detection_follows_wrapped_driver_error(self):
        driver_error = OperationalError(1040, "Too many connections")
        wrapped = RuntimeError("database operation failed")
        wrapped.__cause__ = driver_error

        self.assertTrue(emo_ws._is_database_connection_exhausted(driver_error))
        self.assertTrue(emo_ws._is_database_connection_exhausted(wrapped))
        self.assertFalse(
            emo_ws._is_database_connection_exhausted(
                OperationalError(2006, "Server has gone away")
            )
        )


if __name__ == "__main__":
    unittest.main()
