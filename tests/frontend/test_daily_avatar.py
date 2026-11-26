import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import patch

from PIL import Image

from supysonic.account_avatars import account_avatar_path, avatar_png, publish_avatar
from supysonic.avatar_generation import AvatarGenerationError, ENDPOINT
from supysonic.daily_avatar import rotate_daily_avatar
from supysonic.db import User, close_connection
from .frontendtestbase import FrontendTestBase


class DailyAvatarTests(FrontendTestBase):
    def setUp(self):
        super().setUp()
        self.config = self.client.application.config
        self.config["BASE"] = dict(self.config["BASE"], tempdatafolder=self.config["WEBAPP"]["cache_dir"])
        self.config["RECOMMENDATION_AGENT"] = dict(
            self.config["RECOMMENDATION_AGENT"], enabled=True, provider="openai",
            api_base_url=ENDPOINT, daily_avatar_username="bob", daily_avatar_subject="male listener",
        )
        self.user = User.get(User.name == "bob")
        self.current = account_avatar_path(self.config, self.user)
        self.morning = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc)
        self.draw = self.enter_draw()
        self.draw.return_value = self.png("blue")

    def enter_draw(self):
        mock = patch("supysonic.daily_avatar.generate_avatar")
        self.addCleanup(mock.stop)
        return mock.start()

    @staticmethod
    def png(color):
        with Image.new("RGB", (70, 70), color) as image, io.BytesIO() as output:
            image.save(output, "PNG")
            return output.getvalue()

    def test_calendar_scope_profile_and_exact_history(self):
        previous = avatar_png(self.png("red"))
        publish_avatar(self.current, data=previous)
        self.assertEqual(rotate_daily_avatar(self.config, self.morning - timedelta(seconds=1)), "idle")
        with patch("supysonic.daily_avatar.build_profile_home",
                   return_value={"moods": [{"value": "gentle"}], "tags": [{"value": "piano"}]}):
            self.assertEqual(rotate_daily_avatar(self.config, self.morning), "applied")
        self.assertIn("male listener", self.draw.call_args.args[2])
        self.assertIn("gentle, piano", self.draw.call_args.args[2])
        self.assertEqual(rotate_daily_avatar(self.config, self.morning + timedelta(hours=15)), "idle")
        self.draw.assert_called_once()
        history = self.current.parent / "history" / self.current.stem
        self.assertEqual([p.read_bytes() for p in history.glob("*.png")], [previous])
        first = self.current.read_bytes()
        self.draw.return_value = self.png("green")
        self.assertEqual(rotate_daily_avatar(self.config, self.morning + timedelta(days=1)), "applied")
        publish_avatar(self.current)
        self.assertEqual({p.read_bytes() for p in history.glob("*.png")},
                         {previous, first, avatar_png(self.png("green"))})
        other = User.get(User.name == "alice")
        self.assertFalse(account_avatar_path(self.config, other).exists())
        self.config["RECOMMENDATION_AGENT"]["daily_avatar_username"] = ""
        self.assertEqual(rotate_daily_avatar(self.config, self.morning + timedelta(days=2)), "idle")
        self.assertEqual(self.draw.call_count, 2)

    def test_uncertain_request_is_not_repeated_but_budget_admission_can_retry(self):
        publish_avatar(self.current, data=avatar_png(self.png("red")))
        previous = self.current.read_bytes()
        self.draw.side_effect = [AvatarGenerationError("budget"), AvatarGenerationError("network")]
        for _ in range(2):
            with self.assertRaises(AvatarGenerationError):
                rotate_daily_avatar(self.config, self.morning)
        self.assertEqual(rotate_daily_avatar(self.config, self.morning), "idle")
        self.assertEqual(self.current.read_bytes(), previous)
        self.assertEqual(self.draw.call_count, 2)

    def test_staged_image_survives_archive_failure_and_restart(self):
        previous = avatar_png(self.png("red"))
        publish_avatar(self.current, data=previous)
        with patch("supysonic.account_avatars.write_atomic", side_effect=OSError):
            with self.assertRaises(OSError):
                rotate_daily_avatar(self.config, self.morning)
        self.assertEqual(self.current.read_bytes(), previous)
        self.assertEqual(rotate_daily_avatar(self.config, self.morning), "applied")
        self.assertEqual(self.current.read_bytes(), avatar_png(self.png("blue")))
        self.draw.assert_called_once()

    def test_second_worker_cannot_issue_a_second_request(self):
        entered, resume = Event(), Event()
        def draw(*_):
            entered.set()
            self.assertTrue(resume.wait(5))
            return self.png("blue")
        self.draw.side_effect = draw
        def worker():
            try:
                return rotate_daily_avatar(self.config, self.morning)
            finally:
                close_connection()
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(worker)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(rotate_daily_avatar(self.config, self.morning), "idle")
            finally:
                resume.set()
            self.assertEqual(future.result(), "applied")
        self.draw.assert_called_once()
