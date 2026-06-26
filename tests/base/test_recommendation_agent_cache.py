import unittest
from pathlib import Path

from supysonic import db
from supysonic.recommendation_agent import _build_agent_cache_context_hash
from supysonic.recommendation_agent_cache import (
    build_recommendation_agent_context_hash,
    get_cached_recommendation_agent_payload,
    save_recommendation_agent_cache_payload,
)

from ..testbase import TestBase


class RecommendationAgentCacheTestCase(TestBase):
    def _agent_cache_unique_indexes(self):
        indexes = []
        for index in db.db.execute_sql(
            "PRAGMA index_list(recommendation_agent_cache)"
        ).fetchall():
            if not index[2]:
                continue
            columns = [
                row[2]
                for row in db.db.execute_sql(
                    f"PRAGMA index_info({index[1]})"
                ).fetchall()
            ]
            indexes.append(tuple(columns))
        return indexes

    def test_agent_cache_table_exists_in_packaged_schema(self):
        columns = {
            row[1]
            for row in db.db.execute_sql(
                "PRAGMA table_info(recommendation_agent_cache)"
            ).fetchall()
        }

        self.assertIn("user_id", columns)
        self.assertIn("context_hash", columns)
        self.assertIn("payload_json", columns)
        self.assertIn("expires_at", columns)
        self.assertIn(
            ("user_id", "context_hash"),
            self._agent_cache_unique_indexes(),
        )

    def test_sqlite_agent_cache_migration_creates_table(self):
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
                "PRAGMA table_info(recommendation_agent_cache)"
            ).fetchall()
        }
        self.assertIn("context_hash", columns)
        self.assertIn("expires_at", columns)
        self.assertIn(
            ("user_id", "context_hash"),
            self._agent_cache_unique_indexes(),
        )

    def test_save_and_read_non_expired_cache_payload(self):
        user = db.User.get(db.User.name == "alice")
        context_hash = build_recommendation_agent_context_hash(
            {"user": str(user.id), "message": "hello"}
        )
        payload = {"ok": True, "reply": "cached reply", "recommendedArtists": []}

        save_recommendation_agent_cache_payload(
            user,
            context_hash,
            "hello",
            "en",
            "test-model",
            payload,
            ttl_seconds=60,
        )

        self.assertEqual(
            get_cached_recommendation_agent_payload(user, context_hash)["reply"],
            "cached reply",
        )

    def test_cache_payload_is_scoped_to_user(self):
        alice = db.User.get(db.User.name == "alice")
        bob = db.User.get(db.User.name == "bob")
        context_hash = build_recommendation_agent_context_hash(
            {"message": "same hash for isolation test"}
        )

        save_recommendation_agent_cache_payload(
            alice,
            context_hash,
            "same question",
            "en",
            "test-model",
            {"ok": True, "reply": "alice cached reply"},
            ttl_seconds=60,
        )

        save_recommendation_agent_cache_payload(
            bob,
            context_hash,
            "same question",
            "en",
            "test-model",
            {"ok": True, "reply": "bob cached reply"},
            ttl_seconds=60,
        )

        self.assertEqual(
            get_cached_recommendation_agent_payload(bob, context_hash)["reply"],
            "bob cached reply",
        )
        self.assertIsNone(
            get_cached_recommendation_agent_payload(None, context_hash)
        )
        self.assertEqual(
            get_cached_recommendation_agent_payload(alice, context_hash)["reply"],
            "alice cached reply",
        )

    def test_context_hash_includes_recommendation_track_summary(self):
        user = db.User.get(db.User.name == "alice")
        base_context = {
            "history": {},
            "playHistory": [],
            "recommendationFeedback": {},
            "recommendationSummary": {"trackCount": 1},
            "currentRecommendationTracks": [
                {
                    "id": "track-1",
                    "title": "Morning Static",
                    "artist": "Local Artist",
                    "album": "First Album",
                    "genre": "rock",
                    "playCount": 0,
                }
            ],
        }
        changed_context = {
            **base_context,
            "currentRecommendationTracks": [
                {
                    "id": "track-1",
                    "title": "Late Night Static",
                    "artist": "Different Local Artist",
                    "album": "Second Album",
                    "genre": "ambient",
                    "playCount": 7,
                }
            ],
        }

        self.assertNotEqual(
            _build_agent_cache_context_hash(
                user,
                "recommend outside artists",
                "en",
                "test-model",
                base_context,
            ),
            _build_agent_cache_context_hash(
                user,
                "recommend outside artists",
                "en",
                "test-model",
                changed_context,
            ),
        )

    def test_context_hash_includes_play_history_beyond_recent_summary(self):
        user = db.User.get(db.User.name == "alice")

        def make_play_history(last_track_title):
            tracks = [
                {
                    "id": f"history-{index}",
                    "title": f"Recent Track {index}",
                    "artist": "Same Artist",
                    "album": "Same Album",
                    "genre": "rock",
                    "duration": 180,
                    "playCount": 0,
                    "playedAt": f"2026-06-25T10:0{index}:00",
                }
                for index in range(5)
            ]
            tracks.append(
                {
                    "id": "history-older",
                    "title": last_track_title,
                    "artist": "Same Artist",
                    "album": "Same Album",
                    "genre": "rock",
                    "duration": 180,
                    "playCount": 0,
                    "playedAt": "2026-06-25T09:00:00",
                }
            )
            return tracks

        base_context = {
            "history": {
                "activityCount": 6,
                "topArtists": [{"name": "Same Artist", "playCount": 6}],
                "favoriteGenres": [{"name": "rock", "playCount": 6}],
                "recentTracks": make_play_history("Older Track A")[:5],
            },
            "playHistory": make_play_history("Older Track A"),
            "recommendationFeedback": {},
            "recommendationSummary": {"trackCount": 0},
            "currentRecommendationTracks": [],
        }
        changed_context = {
            **base_context,
            "playHistory": make_play_history("Older Track B"),
        }

        self.assertNotEqual(
            _build_agent_cache_context_hash(
                user,
                "recommend outside artists",
                "en",
                "test-model",
                base_context,
            ),
            _build_agent_cache_context_hash(
                user,
                "recommend outside artists",
                "en",
                "test-model",
                changed_context,
            ),
        )

    def test_zero_ttl_does_not_save_cache_payload(self):
        user = db.User.get(db.User.name == "alice")
        context_hash = build_recommendation_agent_context_hash({"message": "no cache"})

        save_recommendation_agent_cache_payload(
            user,
            context_hash,
            "no cache",
            "en",
            "test-model",
            {"ok": True},
            ttl_seconds=0,
        )

        self.assertIsNone(get_cached_recommendation_agent_payload(user, context_hash))


if __name__ == "__main__":
    unittest.main()
