import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from playhouse.db_url import connect as connect_database

from supysonic import db


class EmoSchemaMigrationTestCase(unittest.TestCase):
    @staticmethod
    def _record_external_evidence(
        provider: str,
        phase: str,
        schema_version: str,
    ) -> None:
        evidence_path = os.environ.get("SUPYSONIC_MIGRATION_EVIDENCE_FILE")
        if not evidence_path:
            return
        with open(evidence_path, "a", encoding="utf-8") as evidence_file:
            evidence_file.write(
                "%s %s schema_version=%s\n"
                % (provider, phase, schema_version)
            )

    @staticmethod
    def _reset_external_database(provider: str, database_uri: str) -> None:
        database = connect_database(database_uri)
        database.connect()
        try:
            if provider == "postgres":
                database.execute_sql("DROP SCHEMA public CASCADE")
                database.execute_sql("CREATE SCHEMA public")
                database.execute_sql("CREATE EXTENSION IF NOT EXISTS citext")
            else:
                database.execute_sql("SET FOREIGN_KEY_CHECKS = 0")
                tables = database.execute_sql("SHOW TABLES").fetchall()
                for (table_name,) in tables:
                    escaped_name = table_name.replace("`", "``")
                    database.execute_sql("DROP TABLE `%s`" % escaped_name)
                database.execute_sql("SET FOREIGN_KEY_CHECKS = 1")
        finally:
            database.close()

    @staticmethod
    def _create_external_upgrade_fixture(
        provider: str,
        database_uri: str,
    ) -> None:
        database = connect_database(database_uri)
        database.connect()
        try:
            key_column = '"key"' if provider == "postgres" else "`key`"
            database.execute_sql(
                "CREATE TABLE meta ("
                "%s VARCHAR(32) PRIMARY KEY, value VARCHAR(256) NOT NULL)"
                % key_column
            )
            database.execute_sql(
                "INSERT INTO meta (%s, value) "
                "VALUES ('schema_version', '20260708')" % key_column
            )
            id_type = "UUID" if provider == "postgres" else "CHAR(36)"
            timestamp_type = "TIMESTAMP" if provider == "postgres" else "DATETIME"
            database.execute_sql(
                """
                CREATE TABLE emo_playback_context (
                    id %s PRIMARY KEY,
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
                    created_at %s NOT NULL,
                    updated_at %s NOT NULL
                )
                """ % (id_type, timestamp_type, timestamp_type)
            )
            database.execute_sql(
                """
                INSERT INTO emo_playback_context (
                    id, playback_context_id, user_name, authority_client_id,
                    origin_client_id, queue_json, current_index, track_id,
                    state, position_ms, queue_revision, control_version,
                    version, epoch, created_at, updated_at
                ) VALUES (
                    '00000000-0000-0000-0000-000000000001',
                    'context-1', 'alice', 'phone-1', 'phone-1',
                    '[\"song-2\",\"song-1\"]', 0, 'song-2', 'closed', 1200,
                    0, 0, 0, 0,
                    '2026-07-08 00:00:00', '2026-07-08 00:01:00'
                )
                """
            )
        finally:
            database.close()

    def _assert_external_provider_migration(
        self,
        provider: str,
        database_uri: str,
    ) -> None:
        required_fields = {
            "authority_device_session_id",
            "timeline_id",
            "creation_fingerprint",
            "lifecycle",
            "closed_at",
        }
        initialized = False
        database_available = False
        try:
            self._reset_external_database(provider, database_uri)
            database_available = True
            db.init_database(database_uri)
            initialized = True
            columns = {
                row[0]
                for row in db.db.execute_sql(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'emo_playback_context'"
                ).fetchall()
            }
            self.assertTrue(required_fields.issubset(columns))
            self.assertEqual(db.Meta["schema_version"].value, "20260712")
            self._record_external_evidence(
                provider,
                "clean",
                db.Meta["schema_version"].value,
            )
            db.release_database()
            initialized = False

            self._reset_external_database(provider, database_uri)
            self._create_external_upgrade_fixture(provider, database_uri)
            db.init_database(database_uri)
            initialized = True
            row = db.db.execute_sql(
                "SELECT authority_device_session_id, timeline_id, "
                "creation_fingerprint, lifecycle, state, queue_revision, "
                "control_version, version, epoch, closed_at "
                "FROM emo_playback_context "
                "WHERE playback_context_id = 'context-1'"
            ).fetchone()

            self.assertIsNone(row[0])
            self.assertEqual(row[1], "playback:context-1")
            self.assertIsNone(row[2])
            self.assertEqual(row[3], "closed")
            self.assertEqual(row[4], "stopped")
            self.assertEqual(row[5:9], (1, 1, 1, 1))
            self.assertIsNotNone(row[9])
            self.assertEqual(db.Meta["schema_version"].value, "20260712")
            self._record_external_evidence(
                provider,
                "upgrade_from_20260708",
                db.Meta["schema_version"].value,
            )
        finally:
            if initialized:
                db.release_database()
            if database_available:
                self._reset_external_database(provider, database_uri)

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

    def test_postgres_runtime_clean_schema_and_20260708_upgrade(self):
        database_uri = os.environ.get("SUPYSONIC_TEST_POSTGRES_URI")
        if not database_uri:
            self.skipTest("SUPYSONIC_TEST_POSTGRES_URI is not configured")
        self._assert_external_provider_migration("postgres", database_uri)

    def test_mysql_runtime_clean_schema_and_20260708_upgrade(self):
        database_uri = os.environ.get("SUPYSONIC_TEST_MYSQL_URI")
        if not database_uri:
            self.skipTest("SUPYSONIC_TEST_MYSQL_URI is not configured")
        self._assert_external_provider_migration("mysql", database_uri)


if __name__ == "__main__":
    unittest.main()
