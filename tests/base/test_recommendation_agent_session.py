import unittest
from pathlib import Path

from supysonic import db
from supysonic.recommendation_agent_session import (
    clear_recommendation_agent_sessions,
    latest_recommended_artists_from_sessions,
    list_recommendation_agent_sessions,
    save_recommendation_agent_session,
)

from ..testbase import TestBase


class RecommendationAgentSessionTestCase(TestBase):
    def test_agent_session_table_exists_in_packaged_schema(self):
        columns = {
            row[1]
            for row in db.db.execute_sql(
                "PRAGMA table_info(recommendation_agent_session)"
            ).fetchall()
        }

        self.assertIn("user_id", columns)
        self.assertIn("message", columns)
        self.assertIn("reply", columns)
        self.assertIn("recommended_artists_json", columns)
        self.assertIn("context_summary_json", columns)

    def test_sqlite_agent_session_migration_creates_table(self):
        migration_root = (
            Path(__file__).resolve().parents[2]
            / "supysonic"
            / "schema"
            / "migration"
            / "sqlite"
        )

        db.db.execute_sql("DROP TABLE IF EXISTS recommendation_agent_cache")
        db.db.execute_sql("DROP TABLE IF EXISTS recommendation_agent_session")
        db.db.execute_sql("DROP TABLE IF EXISTS user_recommendation_feedback")
        for migration_name in ("20260524.sql", "20260625.sql"):
            migration_path = migration_root / migration_name
            for statement in migration_path.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    db.db.execute_sql(statement)

        columns = {
            row[1]
            for row in db.db.execute_sql(
                "PRAGMA table_info(recommendation_agent_session)"
            ).fetchall()
        }
        self.assertIn("recommended_artists_json", columns)
        self.assertIn("created_at", columns)

    def test_save_list_and_clear_agent_sessions(self):
        user = db.User.get(db.User.name == "alice")

        first = save_recommendation_agent_session(
            user,
            "first question",
            "first reply",
            [
                {
                    "name": "First Artist",
                    "reason": "reason",
                    "genres": ["rock"],
                    "starterTracks": ["Song A"],
                }
            ],
            {"history": {"activityCount": 1}},
            "test-model",
            "en",
        )
        save_recommendation_agent_session(
            user,
            "second question",
            "second reply",
            [],
            {},
            "test-model",
            "en",
        )

        sessions = list_recommendation_agent_sessions(user)
        first_session = next(
            session for session in sessions if session["id"] == first["id"]
        )

        self.assertEqual(len(sessions), 2)
        self.assertEqual(first_session["message"], "first question")
        self.assertEqual(first_session["recommendedArtists"][0]["name"], "First Artist")
        self.assertEqual(
            latest_recommended_artists_from_sessions(sessions)[0]["name"],
            "First Artist",
        )
        self.assertEqual(clear_recommendation_agent_sessions(user), 2)
        self.assertEqual(list_recommendation_agent_sessions(user), [])

    def test_latest_recommended_artists_skips_sessions_without_named_artists(self):
        sessions = [
            {
                "message": "first",
                "recommendedArtists": [{"name": "Earlier Artist"}],
            },
            {
                "message": "latest malformed",
                "recommendedArtists": [{"reason": "missing name"}, "bad"],
            },
        ]

        artists = latest_recommended_artists_from_sessions(sessions)

        self.assertEqual(artists[0]["name"], "Earlier Artist")

    def test_agent_sessions_are_isolated_per_user(self):
        alice = db.User.get(db.User.name == "alice")
        bob = db.User.get(db.User.name == "bob")

        save_recommendation_agent_session(
            alice,
            "alice question",
            "alice reply",
            [{"name": "Alice Artist"}],
            {},
            "test-model",
            "en",
        )
        save_recommendation_agent_session(
            bob,
            "bob question",
            "bob reply",
            [{"name": "Bob Artist"}],
            {},
            "test-model",
            "en",
        )

        self.assertEqual(
            [session["message"] for session in list_recommendation_agent_sessions(alice)],
            ["alice question"],
        )
        self.assertEqual(clear_recommendation_agent_sessions(alice), 1)
        self.assertEqual(list_recommendation_agent_sessions(alice), [])
        self.assertEqual(
            [session["message"] for session in list_recommendation_agent_sessions(bob)],
            ["bob question"],
        )


if __name__ == "__main__":
    unittest.main()
