import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from supysonic import db


class EmoSchemaMigrationTestCase(unittest.TestCase):
    def test_sqlite_20260708_upgrade_preserves_context_and_normalizes_cursors(self):
        handle, path = tempfile.mkstemp()
        os.close(handle)
        try:
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE meta (key CHAR(32) PRIMARY KEY, value CHAR(256) NOT NULL);
                INSERT INTO meta (key, value) VALUES ('schema_version', '20260708');
                CREATE TABLE emo_playback_context (
                    id CHAR(36) PRIMARY KEY,
                    playback_context_id VARCHAR(128) NOT NULL UNIQUE,
                    user_name VARCHAR(64) NOT NULL,
                    authority_client_id VARCHAR(128),
                    origin_client_id VARCHAR(128),
                    queue_json TEXT NOT NULL,
                    current_index INTEGER NOT NULL DEFAULT 0,
                    track_id VARCHAR(128),
                    state VARCHAR(32) NOT NULL DEFAULT 'stopped',
                    position_ms INTEGER NOT NULL DEFAULT 0,
                    volume INTEGER,
                    queue_revision INTEGER NOT NULL DEFAULT 1,
                    control_version INTEGER NOT NULL DEFAULT 1,
                    version INTEGER NOT NULL DEFAULT 1,
                    epoch INTEGER NOT NULL DEFAULT 1,
                    playback_json TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                );
                INSERT INTO emo_playback_context (
                    id, playback_context_id, user_name, authority_client_id,
                    origin_client_id, queue_json, current_index, track_id,
                    state, position_ms, queue_revision, control_version,
                    version, epoch, created_at, updated_at
                ) VALUES (
                    'context-row-1', 'context-1', 'alice', 'phone-1',
                    'phone-1', '["song-2","song-1"]', 0, 'song-2',
                    'closed', 1200, 0, 0, 0, 0,
                    '2026-07-08 00:00:00', '2026-07-08 00:01:00'
                );
                """
            )
            connection.close()

            db.init_database("sqlite:///" + path)
            row = db.db.execute_sql(
                "SELECT authority_device_session_id, timeline_id, "
                "creation_fingerprint, lifecycle, state, queue_revision, "
                "control_version, version, epoch, closed_at "
                "FROM emo_playback_context WHERE playback_context_id = 'context-1'"
            ).fetchone()

            self.assertIsNone(row[0])
            self.assertEqual(row[1], "playback:context-1")
            self.assertIsNone(row[2])
            self.assertEqual(row[3], "closed")
            self.assertEqual(row[4], "stopped")
            self.assertEqual(row[5:9], (1, 1, 1, 1))
            self.assertIsNotNone(row[9])
            self.assertEqual(db.Meta["schema_version"].value, "20260712")
        finally:
            db.release_database()
            os.remove(path)

    def test_all_provider_schemas_and_migrations_declare_strict_context_fields(self):
        root = Path(__file__).resolve().parents[2] / "supysonic" / "schema"
        required_fields = {
            "authority_device_session_id",
            "timeline_id",
            "creation_fingerprint",
            "lifecycle",
            "closed_at",
        }

        for provider in ("sqlite", "postgres", "mysql"):
            with self.subTest(provider=provider):
                base_schema = (root / (provider + ".sql")).read_text("utf-8")
                migration = (
                    root / "migration" / provider / "20260712.sql"
                ).read_text("utf-8")
                for field_name in required_fields:
                    self.assertIn(field_name, base_schema)
                    self.assertIn(field_name, migration)
                self.assertIn("queue_revision", migration)
                self.assertIn("control_version", migration)
                self.assertIn("version", migration)
                self.assertIn("epoch", migration)


if __name__ == "__main__":
    unittest.main()
