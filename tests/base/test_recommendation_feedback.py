import unittest
from pathlib import Path
from types import SimpleNamespace

from supysonic import db
from supysonic.recommendation_feedback import (
    HOT_RECOMMENDED_SCOPE,
    MAX_RECOMMENDATION_FEEDBACK_TARGET_ID_LENGTH,
    get_disliked_recommended_song_ids,
    get_recommendation_feedback_preferences,
    set_recommendation_feedback,
    track_matches_negative_recommendation_feedback,
)

from ..testbase import TestBase


class RecommendationFeedbackTestCase(TestBase):
    def test_feedback_table_exists_in_packaged_schema(self):
        column_rows = db.db.execute_sql(
            "PRAGMA table_info(user_recommendation_feedback)"
        ).fetchall()
        columns = {row[1] for row in column_rows}
        column_types = {row[1]: row[2] for row in column_rows}

        self.assertIn("user_id", columns)
        self.assertIn("song_id", columns)
        self.assertIn("target_type", columns)
        self.assertIn("target_id", columns)
        self.assertIn("scope", columns)
        self.assertIn("deleted_at", columns)
        self.assertEqual(column_types["song_id"], "VARCHAR(128)")
        self.assertEqual(column_types["target_id"], "VARCHAR(128)")

    def test_sqlite_feedback_migration_creates_table(self):
        migration_path = (
            Path(__file__).resolve().parents[2]
            / "supysonic"
            / "schema"
            / "migration"
            / "sqlite"
            / "20260524.sql"
        )

        db.db.execute_sql("DROP TABLE IF EXISTS user_recommendation_feedback")
        for statement in migration_path.read_text(encoding="utf-8").split(";"):
            if statement.strip():
                db.db.execute_sql(statement)

        columns = {
            row[1]
            for row in db.db.execute_sql(
                "PRAGMA table_info(user_recommendation_feedback)"
            ).fetchall()
        }
        self.assertIn("song_id", columns)
        self.assertIn("deleted_at", columns)

    def test_sqlite_agent_migration_upgrades_feedback_targets(self):
        migration_root = (
            Path(__file__).resolve().parents[2]
            / "supysonic"
            / "schema"
            / "migration"
            / "sqlite"
        )

        db.db.execute_sql("DROP TABLE IF EXISTS user_recommendation_feedback")
        for migration_name in ("20260524.sql", "20260625.sql"):
            migration_path = migration_root / migration_name
            for statement in migration_path.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    db.db.execute_sql(statement)

        column_rows = db.db.execute_sql(
            "PRAGMA table_info(user_recommendation_feedback)"
        ).fetchall()
        columns = {row[1] for row in column_rows}
        column_types = {row[1]: row[2] for row in column_rows}
        self.assertIn("target_type", columns)
        self.assertIn("target_id", columns)
        self.assertEqual(column_types["target_id"], "VARCHAR(128)")

    def test_dislike_and_restore_are_idempotent(self):
        user = db.User.get(db.User.name == "alice")
        song_id = "song-123"

        set_recommendation_feedback(user, song_id, "dislike")
        set_recommendation_feedback(user, song_id, "dislike")

        self.assertEqual(db.UserRecommendationFeedback.select().count(), 1)
        self.assertEqual(
            get_disliked_recommended_song_ids(user, HOT_RECOMMENDED_SCOPE),
            {song_id},
        )

        set_recommendation_feedback(user, song_id, "restore")
        set_recommendation_feedback(user, song_id, "restore")

        feedback = db.UserRecommendationFeedback.get()
        self.assertEqual(db.UserRecommendationFeedback.select().count(), 1)
        self.assertEqual(feedback.action, "restore")
        self.assertIsNotNone(feedback.deleted_at)
        self.assertEqual(
            get_disliked_recommended_song_ids(user, HOT_RECOMMENDED_SCOPE),
            set(),
        )

    def test_hide_artist_and_restore_are_idempotent(self):
        user = db.User.get(db.User.name == "alice")
        artist_id = "artist-123"

        first = set_recommendation_feedback(user, artist_id, "hide_artist")
        second = set_recommendation_feedback(user, artist_id, "hide_artist")

        self.assertEqual(db.UserRecommendationFeedback.select().count(), 1)
        self.assertEqual(first.target_type, "artist")
        self.assertEqual(second.target_id, artist_id)
        self.assertEqual(
            get_recommendation_feedback_preferences(user)["hidden_artist_ids"],
            {artist_id},
        )

        set_recommendation_feedback(user, artist_id, "restore_artist")
        set_recommendation_feedback(user, artist_id, "restore_artist")

        feedback = db.UserRecommendationFeedback.get()
        self.assertEqual(db.UserRecommendationFeedback.select().count(), 1)
        self.assertEqual(feedback.action, "restore_artist")
        self.assertIsNotNone(feedback.deleted_at)
        self.assertEqual(
            get_recommendation_feedback_preferences(user)["hidden_artist_ids"],
            set(),
        )

    def test_agent_artist_feedback_accepts_sanitized_artist_name_length(self):
        user = db.User.get(db.User.name == "alice")
        artist_name = "A" * 120

        feedback = set_recommendation_feedback(
            user,
            artist_name,
            "hide_artist",
            target_type="artist",
            source="web_agent",
        )

        self.assertEqual(feedback.target_id, artist_name)
        self.assertEqual(feedback.song_id, artist_name)
        self.assertEqual(
            get_recommendation_feedback_preferences(user)["hidden_artist_ids"],
            {artist_name},
        )

        with self.assertRaisesRegex(ValueError, "id is too long"):
            set_recommendation_feedback(
                user,
                "B" * (MAX_RECOMMENDATION_FEEDBACK_TARGET_ID_LENGTH + 1),
                "hide_artist",
                target_type="artist",
            )

    def test_artist_name_feedback_matches_track_artist_name_case_insensitively(self):
        track = SimpleNamespace(
            id="song-1",
            artist_id="00000000-0000-0000-0000-000000000001",
            album_id=None,
            genre="rock",
            artist=SimpleNamespace(
                name="hidden artist",
                get_artist_name=lambda: "Hidden Artist",
            ),
        )
        preferences = {
            "disliked_song_ids": set(),
            "hidden_artist_ids": {"  HIDDEN   ARTIST "},
            "hidden_album_ids": set(),
            "hidden_genres": set(),
        }

        self.assertTrue(
            track_matches_negative_recommendation_feedback(track, preferences)
        )

    def test_action_rejects_mismatched_target_type(self):
        user = db.User.get(db.User.name == "alice")

        with self.assertRaisesRegex(
            ValueError,
            "target type does not match action",
        ):
            set_recommendation_feedback(
                user,
                "artist-123",
                "hide_artist",
                target_type="song",
            )

        self.assertEqual(db.UserRecommendationFeedback.select().count(), 0)

    def test_generic_restore_accepts_explicit_target_type(self):
        user = db.User.get(db.User.name == "alice")

        set_recommendation_feedback(user, "artist-123", "hide_artist")
        set_recommendation_feedback(
            user,
            "artist-123",
            "restore",
            target_type="artist",
        )

        feedback = db.UserRecommendationFeedback.get()
        self.assertEqual(db.UserRecommendationFeedback.select().count(), 1)
        self.assertEqual(feedback.target_type, "artist")
        self.assertEqual(feedback.action, "restore")
        self.assertIsNotNone(feedback.deleted_at)
        self.assertEqual(
            get_recommendation_feedback_preferences(user)["hidden_artist_ids"],
            set(),
        )

    def test_album_style_and_like_more_preferences_are_collected(self):
        user = db.User.get(db.User.name == "alice")

        set_recommendation_feedback(user, "album-123", "hide_album")
        set_recommendation_feedback(user, "Rock", "not_this_style")
        set_recommendation_feedback(user, "song-456", "like_more")

        preferences = get_recommendation_feedback_preferences(user)

        self.assertEqual(preferences["hidden_album_ids"], {"album-123"})
        self.assertEqual(preferences["hidden_genres"], {"rock"})
        self.assertEqual(preferences["liked_more_song_ids"], {"song-456"})

    def test_style_feedback_normalizes_target_id_case(self):
        user = db.User.get(db.User.name == "alice")

        set_recommendation_feedback(user, "Rock", "not_this_style")
        set_recommendation_feedback(user, "rock", "not_this_style")

        feedback = db.UserRecommendationFeedback.get()
        self.assertEqual(db.UserRecommendationFeedback.select().count(), 1)
        self.assertEqual(feedback.target_type, "genre")
        self.assertEqual(feedback.target_id, "rock")
        self.assertEqual(
            get_recommendation_feedback_preferences(user)["hidden_genres"],
            {"rock"},
        )

        set_recommendation_feedback(user, "ROCK", "restore_style")

        feedback = db.UserRecommendationFeedback.get()
        self.assertEqual(db.UserRecommendationFeedback.select().count(), 1)
        self.assertEqual(feedback.action, "restore_style")
        self.assertIsNotNone(feedback.deleted_at)
        self.assertEqual(
            get_recommendation_feedback_preferences(user)["hidden_genres"],
            set(),
        )


if __name__ == "__main__":
    unittest.main()
