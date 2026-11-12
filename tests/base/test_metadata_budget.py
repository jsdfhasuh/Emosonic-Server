from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import gc
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from peewee import OperationalError, SqliteDatabase

from supysonic.db_layer.core import Meta, db
from supysonic.metadata_budget import (
    BudgetExceeded,
    BudgetUnavailable,
    MetadataBudget,
    MICRO_USD,
    SHANGHAI,
)


class MetadataBudgetTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = SqliteDatabase(
            str(Path(self.directory.name) / "budget.sqlite"),
            timeout=2,
            pragmas={"journal_mode": "wal"},
        )
        self.previous_database = db.obj
        db.initialize(self.database)
        self.database.create_tables([Meta])
        self.current = datetime(2026, 11, 12, 10, 0, tzinfo=SHANGHAI)
        self.config = {
            "monthly_budget_usd": "60", "budget_safety_buffer_usd": "3",
            "daily_budget_usd": "2", "budget_timezone": "Asia/Shanghai",
        }
        self.budget = self.make_budget()

    def tearDown(self):
        self.database.close()
        db.initialize(self.previous_database)
        gc.collect()
        self.directory.cleanup()

    def make_budget(self, **overrides):
        return MetadataBudget(dict(self.config, **overrides), clock=lambda: self.current)

    def test_reserve_and_settle_release_only_unused_amount(self):
        reservation = self.budget.reserve("text", 500_000)
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["month_reserved_micro_usd"], 500_000)
        self.assertEqual(snapshot["day_remaining_micro_usd"], 1_500_000)
        self.budget.settle(reservation, 42_123)
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["month_spent_micro_usd"], 42_123)
        self.assertEqual(snapshot["month_reserved_micro_usd"], 0)
        self.assertEqual(snapshot["day_remaining_micro_usd"], 2_000_000 - 42_123)

    def test_exact_boundary_and_no_partial_month_reservation_on_day_failure(self):
        self.budget.reserve("text", 2 * MICRO_USD)
        before = self.budget.snapshot()
        with self.assertRaises(BudgetExceeded) as caught:
            self.budget.reserve("web", 1)
        self.assertEqual(caught.exception.reason, "day_limit")
        self.assertEqual(caught.exception.next_retry_at, datetime(2026, 11, 13, tzinfo=SHANGHAI))
        self.assertEqual(self.budget.snapshot(), before)
        self.assertEqual(Meta.select().where(Meta.key.startswith("mb:r:")).count(), 1)

    def test_monthly_safety_buffer_is_reserved_from_allowance(self):
        budget = self.make_budget(daily_budget_usd="100")
        budget.reserve("text", 57 * MICRO_USD)
        with self.assertRaises(BudgetExceeded) as caught:
            budget.reserve("web", 1)
        self.assertEqual(caught.exception.reason, "month_limit")
        self.assertEqual(caught.exception.reset_at, datetime(2026, 12, 1, tzinfo=SHANGHAI))

    def test_restart_keeps_unknown_reservations(self):
        self.budget.reserve("web", MICRO_USD)
        recreated = self.make_budget()
        self.assertEqual(recreated.snapshot()["day_reserved_micro_usd"], MICRO_USD)
        self.current += timedelta(days=10)
        self.assertEqual(recreated.snapshot()["month_reserved_micro_usd"], MICRO_USD)

    def test_settlement_is_idempotent_and_conflicting_value_is_rejected(self):
        reservation = self.budget.reserve("web", 300_000)
        self.budget.settle(reservation, 42_000)
        before = self.budget.snapshot()
        self.budget.settle(reservation, 42_000)
        self.assertEqual(self.budget.snapshot(), before)
        with self.assertRaises(BudgetUnavailable):
            self.budget.settle(reservation, 1)
        self.assertEqual(self.budget.snapshot(), before)

    def test_settlement_stays_in_original_period_after_rollover(self):
        self.current = datetime(2026, 12, 1, 0, 0, tzinfo=SHANGHAI) - timedelta(seconds=1)
        reservation = self.budget.reserve("web", 300_000)
        self.current += timedelta(seconds=2)
        self.assertEqual(self.budget.snapshot()["month_reserved_micro_usd"], 0)
        self.budget.settle(reservation, 25_000)
        self.assertEqual(self.budget.snapshot()["month_spent_micro_usd"], 0)
        old = json.loads(Meta.get_by_id("mb:m:202611").value)
        self.assertEqual((old["s"], old["r"]), (25_000, 0))

    def test_asia_shanghai_boundary_uses_utc_input_correctly(self):
        self.current = datetime(2026, 11, 30, 16, 0, tzinfo=timezone.utc)
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["month"], "202612")
        self.assertEqual(snapshot["day"], "20261201")

    def test_daily_reset_does_not_reset_month(self):
        self.budget.reserve("text", 2 * MICRO_USD)
        self.current += timedelta(days=1)
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["day_remaining_micro_usd"], 2 * MICRO_USD)
        self.assertEqual(snapshot["month_reserved_micro_usd"], 2 * MICRO_USD)

    def test_overreservation_actual_is_recorded_and_blocks_new_calls(self):
        reservation = self.budget.reserve("web", 100_000)
        self.budget.settle(reservation, 150_000)
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["month_spent_micro_usd"], 150_000)
        self.assertTrue(snapshot["blocked"])
        with self.assertRaises(BudgetExceeded) as caught:
            self.budget.reserve("text", 1)
        self.assertEqual(caught.exception.reason, "month_blocked")

    def test_database_failure_does_not_release_reservation_or_leak_details(self):
        reservation = self.budget.reserve("web", 100_000)
        before = self.budget.snapshot()
        with patch.object(Meta, "update", side_effect=OperationalError("private credentials")), \
                patch("supysonic.metadata_budget.time.sleep"):
            with self.assertRaises(BudgetUnavailable) as caught:
                self.budget.settle(reservation, 50_000)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.budget.snapshot(), before)

    def test_reservations_cannot_be_hidden_in_an_outer_transaction(self):
        with self.database.atomic():
            with self.assertRaises(BudgetUnavailable):
                self.budget.reserve("text", 1)
        self.assertEqual(Meta.select().count(), 0)

    def test_corrupt_bucket_fails_closed(self):
        self.budget.reserve("text", 1)
        for value in ("broken", '{"v":1}', '{"v":1,"s":0,"r":0,"l":1,"b":0}'):
            Meta.update(value=value).where(Meta.key == "mb:m:202611").execute()
            with self.subTest(value=value), self.assertRaises(BudgetUnavailable):
                self.budget.reserve("text", 1)

    def test_temporarily_invisible_period_retries_in_a_new_transaction(self):
        original_read = self.budget._read
        for index, hidden_key in enumerate(("mb:m:202611", "mb:d:20261112"), 1):
            transactions = []

            def read_with_initial_invisibility(key):
                value = original_read(key)
                if key == hidden_key:
                    transaction = self.database.top_transaction()
                    if not transactions or transactions[-1] is not transaction:
                        transactions.append(transaction)
                    if len(transactions) == 1:
                        return None
                return value

            with self.subTest(key=hidden_key), patch.object(
                self.budget, "_read", side_effect=read_with_initial_invisibility,
            ), patch("supysonic.metadata_budget.time.sleep") as sleep:
                self.budget.reserve("text", 100_000)
                sleep.assert_called_once()
                self.assertEqual(len(transactions), 2)
                self.assertIsNot(transactions[0], transactions[1])
            snapshot = self.budget.snapshot()
            self.assertEqual(snapshot["month_reserved_micro_usd"], index * 100_000)
            self.assertEqual(snapshot["day_reserved_micro_usd"], index * 100_000)
            self.assertEqual(Meta.select().where(Meta.key.startswith("mb:r:")).count(), index)

    def test_existing_periods_are_not_reinserted_during_reservation(self):
        self.budget.reserve("text", 100_000)
        with patch.object(Meta, "insert", wraps=Meta.insert) as insert:
            self.budget.reserve("text", 100_000)
        self.assertEqual(insert.call_count, 1)
        self.assertTrue(insert.call_args.kwargs["key"].startswith("mb:r:"))
        self.assertEqual(self.budget.snapshot()["day_reserved_micro_usd"], 200_000)

    def test_persistently_invisible_period_fails_closed_without_reservation(self):
        with patch.object(self.budget, "_read", return_value=None), \
                patch("supysonic.metadata_budget.time.sleep"), \
                self.assertRaises(BudgetUnavailable):
            self.budget.reserve("text", 100_000)
        self.assertEqual(Meta.select().count(), 0)

    def test_missing_settlement_bucket_cannot_release_money(self):
        reservation = self.budget.reserve("text", 100_000)
        Meta.delete().where(Meta.key == reservation.day_key).execute()
        with self.assertRaises(BudgetUnavailable):
            self.budget.settle(reservation, 1)
        month = json.loads(Meta.get_by_id(reservation.month_key).value)
        self.assertEqual(month["r"], 100_000)

    def test_forged_reservation_is_rejected(self):
        reservation = self.budget.reserve("text", 100_000)
        with self.assertRaises(BudgetUnavailable):
            self.budget.settle(replace(reservation, amount_micro_usd=1), 1)
        self.assertEqual(self.budget.snapshot()["month_reserved_micro_usd"], 100_000)

    def test_invalid_amounts_and_configuration_fail_closed(self):
        for value in (0, -1, True, 1.0, None, 10 ** 16):
            with self.subTest(value=value), self.assertRaises(BudgetUnavailable):
                self.budget.reserve("text", value)
        for value in (None, "0", "-1", "NaN", "Infinity", "0.0000001", True):
            with self.subTest(value=value), self.assertRaises(BudgetUnavailable):
                self.make_budget(monthly_budget_usd=value)
        with self.assertRaises(BudgetUnavailable):
            MetadataBudget({})
        with self.assertRaises(BudgetUnavailable):
            self.make_budget(budget_timezone="unknown")
        with self.assertRaises(BudgetUnavailable):
            self.make_budget(budget_safety_buffer_usd="60")

    def test_configuration_cannot_raise_an_existing_period_limit(self):
        self.budget.reserve("text", 1)
        larger = self.make_budget(monthly_budget_usd="100", daily_budget_usd="10")
        self.assertEqual(larger.snapshot()["day_limit_micro_usd"], 2 * MICRO_USD)
        self.assertEqual(larger.snapshot()["month_limit_micro_usd"], 57 * MICRO_USD)

    def test_rejected_reservation_still_persists_lower_limits(self):
        self.budget.reserve("text", MICRO_USD)
        smaller = self.make_budget(daily_budget_usd="0.5", monthly_budget_usd="4")
        with self.assertRaises(BudgetExceeded):
            smaller.reserve("web", 1)
        self.assertEqual(self.budget.snapshot()["day_limit_micro_usd"], 500_000)
        self.assertEqual(self.budget.snapshot()["month_limit_micro_usd"], MICRO_USD)
        self.assertEqual(self.budget.snapshot()["month_reserved_micro_usd"], MICRO_USD)
        with self.assertRaises(BudgetExceeded):
            self.budget.reserve("web", 1)

    def test_limits_and_reservations_fit_existing_meta_schema(self):
        reservation = self.budget.reserve("web", 100)
        self.budget.settle(reservation, 20)
        for row in Meta.select():
            self.assertLessEqual(len(row.key), 32)
            self.assertLessEqual(len(row.value), 256)

    def test_concurrent_reservations_do_not_oversubscribe(self):
        budget = self.make_budget(daily_budget_usd="0.01")

        def reserve_one(index):
            try:
                return budget.reserve("web", 1000)
            except (BudgetExceeded, BudgetUnavailable):
                return None
            finally:
                if not self.database.is_closed():
                    self.database.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve_one, range(40)))
        accepted = [result for result in results if result is not None]
        self.assertEqual(len(accepted), 10)
        self.assertEqual(budget.snapshot()["day_reserved_micro_usd"], 10_000)
        self.assertEqual(budget.snapshot()["month_reserved_micro_usd"], 10_000)

    def test_concurrent_duplicate_settlements_do_not_double_refund(self):
        reservation = self.budget.reserve("web", 100_000)

        def settle_one(index):
            try:
                self.budget.settle(reservation, 25_000)
            finally:
                if not self.database.is_closed():
                    self.database.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(settle_one, range(12)))
        snapshot = self.budget.snapshot()
        self.assertEqual(snapshot["month_reserved_micro_usd"], 0)
        self.assertEqual(snapshot["month_spent_micro_usd"], 25_000)


if __name__ == "__main__":
    unittest.main()
