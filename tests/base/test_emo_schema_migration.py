import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from playhouse.db_url import connect as connect_database

from supysonic import db


class EmoSchemaMigrationTestCase(unittest.TestCase):
    DISCOVERY_INDEX_NAME = "idx_emo_playback_context_binding"
    DISCOVERY_INDEX_COLUMNS = (
        "user_name",
        "lifecycle",
        "authority_client_id",
        "authority_device_session_id",
    )
    BROADCAST_MODELS = (
        db.EmoBroadcast,
        db.EmoBroadcastIntentOutcome,
        db.EmoBroadcastFence,
        db.EmoBroadcastParticipant,
        db.EmoBroadcastRevision,
        db.EmoBroadcastDelivery,
        db.EmoBroadcastFeedbackSettlement,
        db.EmoBroadcastTerminalRecovery,
    )
    BROADCAST_TIME_FIELDS = {
        "emo_broadcast": ("created_at", "updated_at"),
        "emo_broadcast_intent_outcome": ("created_at", "updated_at"),
        "emo_broadcast_fence": ("created_at", "updated_at"),
        "emo_broadcast_participant": ("created_at", "updated_at"),
        "emo_broadcast_revision": ("created_at",),
        "emo_broadcast_delivery": ("created_at", "updated_at"),
        "emo_broadcast_feedback_settlement": ("created_at",),
        "emo_broadcast_terminal_recovery": ("created_at", "updated_at"),
    }
    CORE_MODELS = (
        db.EmoPlaybackContext,
        db.EmoPlaybackControlTransaction,
        db.EmoPlaybackControlReconciliation,
    )
    CLOSE_FIELDS = (
        "close_action",
        "close_request_fingerprint",
        "close_expected_epoch",
        "close_base_version",
        "closed_from_epoch",
        "closed_from_version",
        "final_epoch",
        "final_version",
        "final_queue_revision",
        "final_control_version",
        "close_outcome_json",
    )
    CONTROL_NEW_FIELDS = (
        "requesting_device_session_id",
        "requesting_connection_nonce",
        "requesting_connection_epoch",
        "effective_at_server_ms",
        "execution_eligible_at_ms",
        "error_message",
        "reconciled_by_control_version",
    )
    REQUEST_INDEX = "idx_emo_control_request_generation"
    AUTHORITY_INDEX = "idx_emo_control_authority_generation"
    RECONCILIATION_INDEX = "idx_emo_reconcile_gap"

    @staticmethod
    def _sql_identifier_tokens(sql: str) -> set[str]:
        sql_without_comments = re.sub(
            r"--[^\r\n]*|/\*.*?\*/",
            " ",
            sql,
            flags=re.DOTALL,
        )
        return {
            token.upper()
            for token in re.findall(
                r"[A-Za-z_][A-Za-z0-9_]*",
                sql_without_comments,
            )
        }

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
            database.execute_sql(
                """
                CREATE TABLE emo_device_playback_state (
                    id %s PRIMARY KEY,
                    playback_context_id VARCHAR(128) NOT NULL,
                    device_session_id VARCHAR(128) NOT NULL,
                    owner_client_id VARCHAR(128) NOT NULL,
                    user_name VARCHAR(64) NOT NULL,
                    state VARCHAR(32) NOT NULL,
                    track_id VARCHAR(128),
                    position_ms INTEGER NOT NULL DEFAULT 0,
                    volume INTEGER,
                    is_authority INTEGER NOT NULL DEFAULT 0,
                    mode VARCHAR(32) NOT NULL DEFAULT 'normal',
                    playback_json TEXT,
                    created_at %s NOT NULL,
                    updated_at %s NOT NULL,
                    UNIQUE(playback_context_id, owner_client_id)
                )
                """ % (id_type, timestamp_type, timestamp_type)
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
            self._assert_external_discovery_index(provider)
            self._assert_external_r11_transaction_schema()
            self._assert_external_r18_broadcast_schema()
            self._assert_external_core_schema_parity(provider)
            self.assertEqual(db.Meta["schema_version"].value, "20260807")
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
            self._assert_external_discovery_index(provider)
            self._assert_external_r11_transaction_schema()
            self._assert_external_r18_broadcast_schema()
            self._assert_external_core_schema_parity(provider)
            self.assertEqual(db.Meta["schema_version"].value, "20260807")
            self._record_external_evidence(
                provider,
                "upgrade_from_20260708",
                db.Meta["schema_version"].value,
            )
            db.release_database()
            initialized = False
            self._run_20260728_non_empty_upgrade(provider, database_uri)
        finally:
            if initialized:
                db.release_database()
            if database_available:
                self._reset_external_database(provider, database_uri)

    def _assert_external_discovery_index(self, provider: str) -> None:
        if provider == "postgres":
            row = db.db.execute_sql(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'emo_playback_context' "
                "AND indexname = %s",
                (self.DISCOVERY_INDEX_NAME,),
            ).fetchone()
            self.assertIsNotNone(row)
            for column_name in self.DISCOVERY_INDEX_COLUMNS:
                self.assertIn(column_name, row[0])
            return

        rows = db.db.execute_sql(
            "SELECT column_name FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() "
            "AND table_name = 'emo_playback_context' "
            "AND index_name = %s ORDER BY seq_in_index",
            (self.DISCOVERY_INDEX_NAME,),
        ).fetchall()
        self.assertEqual(
            tuple(row[0] for row in rows),
                self.DISCOVERY_INDEX_COLUMNS,
        )

    def _assert_external_r11_transaction_schema(self) -> None:
        device_columns = {
            row[0]
            for row in db.db.execute_sql(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'emo_device_playback_state'"
            ).fetchall()
        }
        self.assertTrue(
            {"context_epoch", "applied_control_version", "client_seq"}.issubset(
                device_columns
            )
        )
        tables = {
            row[0]
            for row in db.db.execute_sql(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name IN ("
                "'emo_playback_control_transaction', "
                "'emo_playback_prepare_transaction', "
                "'emo_playback_local_intent')"
            ).fetchall()
        }
        self.assertEqual(
            tables,
            {
                "emo_playback_control_transaction",
                "emo_playback_prepare_transaction",
                "emo_playback_local_intent",
            },
        )

    def _assert_external_r18_broadcast_schema(self) -> None:
        expected_tables = {
            model._meta.table_name for model in self.BROADCAST_MODELS
        }
        placeholders = ", ".join(["%s"] * len(expected_tables))
        rows = db.db.execute_sql(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN (%s)" % placeholders,
            tuple(sorted(expected_tables)),
        ).fetchall()
        self.assertEqual({row[0] for row in rows}, expected_tables)
        for model in self.BROADCAST_MODELS:
            columns = {
                row[0]
                for row in db.db.execute_sql(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s",
                    (model._meta.table_name,),
                ).fetchall()
            }
            self.assertEqual(
                columns,
                {field.column_name for field in model._meta.sorted_fields},
                model._meta.table_name,
            )

    def _assert_external_columns_and_nullability(self, provider: str, model) -> None:
        if provider == "postgres":
            rows = db.db.execute_sql(
                "SELECT column_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                (model._meta.table_name,),
            ).fetchall()
        else:
            rows = db.db.execute_sql(
                "SELECT column_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s",
                (model._meta.table_name,),
            ).fetchall()
        actual = {row[0]: row[1] for row in rows}
        expected = {
            field.column_name: field
            for field in model._meta.sorted_fields
        }
        self.assertEqual(set(actual), set(expected), model._meta.table_name)
        for column_name, field in expected.items():
            if field.primary_key:
                self.assertEqual(actual[column_name], "NO")
            elif field.null:
                self.assertEqual(actual[column_name], "YES", column_name)
            else:
                self.assertEqual(actual[column_name], "NO", column_name)

    def _assert_external_named_index(
        self,
        provider: str,
        table_name: str,
        index_name: str,
        columns,
    ) -> None:
        if provider == "postgres":
            row = db.db.execute_sql(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = %s "
                "AND indexname = %s",
                (table_name, index_name),
            ).fetchone()
            self.assertIsNotNone(row, index_name)
            index_definition = row[0].lower()
            for column_name in columns:
                self.assertIn(column_name, index_definition)
            return

        rows = db.db.execute_sql(
            "SELECT column_name FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = %s "
            "AND index_name = %s ORDER BY seq_in_index",
            (table_name, index_name),
        ).fetchall()
        self.assertEqual(tuple(row[0] for row in rows), tuple(columns), index_name)

    def _assert_external_unique_index(self, provider: str, table_name: str, columns):
        if provider == "postgres":
            rows = db.db.execute_sql(
                "SELECT tc.constraint_name, kcu.column_name, "
                "kcu.ordinal_position "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "ON tc.constraint_name = kcu.constraint_name "
                "AND tc.table_schema = kcu.table_schema "
                "AND tc.table_name = kcu.table_name "
                "WHERE tc.table_schema = 'public' AND tc.table_name = %s "
                "AND tc.constraint_type = 'UNIQUE' "
                "ORDER BY tc.constraint_name, kcu.ordinal_position",
                (table_name,),
            ).fetchall()
        else:
            rows = db.db.execute_sql(
                "SELECT index_name, column_name, seq_in_index "
                "FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "AND non_unique = 0 ORDER BY index_name, seq_in_index",
                (table_name,),
            ).fetchall()
        grouped = {}
        for row in rows:
            name = row[0]
            column_name = row[1]
            grouped.setdefault(name, []).append(column_name)
        self.assertIn(tuple(columns), {tuple(value) for value in grouped.values()})

    def _assert_external_core_schema_parity(self, provider: str) -> None:
        for model in self.CORE_MODELS:
            with self.subTest(provider=provider, table=model._meta.table_name):
                self._assert_external_columns_and_nullability(provider, model)
        self._assert_external_named_index(
            provider,
            "emo_playback_control_transaction",
            self.REQUEST_INDEX,
            (
                "requesting_client_id",
                "requesting_device_session_id",
                "requesting_connection_nonce",
                "requesting_connection_epoch",
                "status",
            ),
        )
        self._assert_external_named_index(
            provider,
            "emo_playback_control_transaction",
            self.AUTHORITY_INDEX,
            (
                "authority_client_id",
                "authority_device_session_id",
                "routed_connection_nonce",
                "routed_connection_epoch",
                "status",
            ),
        )
        self._assert_external_named_index(
            provider,
            "emo_playback_control_reconciliation",
            self.RECONCILIATION_INDEX,
            (
                "playback_context_id",
                "epoch",
                "through_control_version",
            ),
        )
        self._assert_external_unique_index(
            provider,
            "emo_playback_control_reconciliation",
            (
                "playback_context_id",
                "epoch",
                "reconciliation_control_version",
            ),
        )

    def _assert_sqlite_core_schema_parity(self) -> None:
        for model in self.CORE_MODELS:
            table_name = model._meta.table_name
            with self.subTest(table=table_name):
                table_info = db.db.execute_sql(
                    "PRAGMA table_info('%s')" % table_name
                ).fetchall()
                actual_columns = {row[1] for row in table_info}
                expected_fields = {
                    field.column_name: field
                    for field in model._meta.sorted_fields
                }
                self.assertEqual(actual_columns, set(expected_fields))
                self.assertEqual(
                    {row[1] for row in table_info if row[5]},
                    {field.column_name for field in expected_fields.values() if field.primary_key},
                )
                for row in table_info:
                    field = expected_fields[row[1]]
                    if field.primary_key:
                        continue
                    self.assertEqual(bool(row[3]), not field.null, row[1])

        def index_columns(index_name):
            return tuple(
                row[2]
                for row in db.db.execute_sql(
                    "PRAGMA index_info('%s')" % index_name
                ).fetchall()
            )

        self.assertEqual(
            index_columns(self.REQUEST_INDEX),
            (
                "requesting_client_id",
                "requesting_device_session_id",
                "requesting_connection_nonce",
                "requesting_connection_epoch",
                "status",
            ),
        )
        self.assertEqual(
            index_columns(self.AUTHORITY_INDEX),
            (
                "authority_client_id",
                "authority_device_session_id",
                "routed_connection_nonce",
                "routed_connection_epoch",
                "status",
            ),
        )
        self.assertEqual(
            index_columns(self.RECONCILIATION_INDEX),
            (
                "playback_context_id",
                "epoch",
                "through_control_version",
            ),
        )

        for table_name, unique_columns in (
            (
                "emo_playback_control_transaction",
                ("playback_context_id", "epoch", "command_control_version"),
            ),
            (
                "emo_playback_control_reconciliation",
                (
                    "playback_context_id",
                    "epoch",
                    "reconciliation_control_version",
                ),
            ),
        ):
            indexes = db.db.execute_sql(
                "PRAGMA index_list('%s')" % table_name
            ).fetchall()
            self.assertIn(
                unique_columns,
                {
                    index_columns(row[1])
                    for row in indexes
                    if row[2]
                },
            )

    @staticmethod
    def _legacy_id(provider: str, number: int) -> str:
        if provider == "postgres":
            return "00000000-0000-0000-0000-%012d" % number
        if provider == "mysql":
            return "%032d" % number
        return "00000000-0000-0000-0000-%012d" % number

    def _create_20260728_fixture(self, provider: str, database) -> None:
        key_column = '"key"' if provider == "postgres" else "`key`" if provider == "mysql" else "key"
        id_type = "UUID" if provider == "postgres" else "CHAR(32)" if provider == "mysql" else "CHAR(36)"
        timestamp_type = "TIMESTAMP" if provider == "postgres" else "DATETIME"
        context_id = self._legacy_id(provider, 1)
        root_id = self._legacy_id(provider, 2)
        dependent_id = self._legacy_id(provider, 3)
        terminal_id = self._legacy_id(provider, 4)
        statements = [
            "CREATE TABLE meta (%s VARCHAR(32) PRIMARY KEY, value VARCHAR(256) NOT NULL)" % key_column,
            "INSERT INTO meta (%s, value) VALUES ('schema_version', '20260728')" % key_column,
            """
            CREATE TABLE emo_playback_context (
                id %s PRIMARY KEY,
                playback_context_id VARCHAR(128) NOT NULL UNIQUE,
                user_name VARCHAR(64) NOT NULL,
                authority_client_id VARCHAR(128),
                authority_device_session_id VARCHAR(128),
                origin_client_id VARCHAR(128),
                timeline_id VARCHAR(128),
                creation_fingerprint VARCHAR(64),
                lifecycle VARCHAR(16) NOT NULL DEFAULT 'active',
                queue_json TEXT NOT NULL,
                current_index INTEGER NOT NULL DEFAULT 0,
                track_id VARCHAR(128),
                state VARCHAR(32) NOT NULL DEFAULT 'idle',
                position_ms INTEGER NOT NULL DEFAULT 0,
                volume INTEGER,
                queue_revision INTEGER NOT NULL DEFAULT 1,
                control_version INTEGER NOT NULL DEFAULT 1,
                version INTEGER NOT NULL DEFAULT 1,
                epoch INTEGER NOT NULL DEFAULT 1,
                playback_json TEXT,
                closed_at %s,
                created_at %s NOT NULL,
                updated_at %s NOT NULL
            )
            """ % (id_type, timestamp_type, timestamp_type, timestamp_type),
            """
            CREATE TABLE emo_playback_control_transaction (
                id %s PRIMARY KEY,
                playback_context_id VARCHAR(128) NOT NULL,
                user_name VARCHAR(64) NOT NULL,
                epoch INTEGER NOT NULL,
                command_control_version INTEGER NOT NULL,
                requesting_client_id VARCHAR(128) NOT NULL,
                authority_client_id VARCHAR(128) NOT NULL,
                authority_device_session_id VARCHAR(128) NOT NULL,
                routed_connection_nonce VARCHAR(128) NOT NULL,
                routed_connection_epoch INTEGER NOT NULL DEFAULT 1,
                action VARCHAR(64) NOT NULL,
                accepted_target_json TEXT NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                error_code VARCHAR(64),
                depends_on_control_version INTEGER,
                accepted_at_ms BIGINT NOT NULL,
                execution_timeout_ms INTEGER NOT NULL,
                watchdog_deadline_at_ms BIGINT NOT NULL,
                applied_control_version INTEGER,
                terminal_fingerprint VARCHAR(64),
                terminal_at_ms BIGINT,
                created_at %s NOT NULL,
                updated_at %s NOT NULL,
                UNIQUE(playback_context_id, epoch, command_control_version)
            )
            """ % (id_type, timestamp_type, timestamp_type),
            """
            INSERT INTO emo_playback_context (
                id, playback_context_id, user_name, authority_client_id,
                authority_device_session_id, origin_client_id, timeline_id,
                creation_fingerprint, lifecycle, queue_json, current_index,
                track_id, state, position_ms, queue_revision, control_version,
                version, epoch, created_at, updated_at
            ) VALUES (
                '%s', 'context-active', 'alice', 'player-1', 'device-1',
                'player-1', 'timeline-active', 'fingerprint-active', 'active',
                '["song-1"]', 0, 'song-1', 'paused', 0, 4, 10, 12, 2,
                '2026-08-07 00:00:00', '2026-08-07 00:01:00'
            )
            """ % context_id,
            """
            INSERT INTO emo_playback_context (
                id, playback_context_id, user_name, authority_client_id,
                authority_device_session_id, origin_client_id, timeline_id,
                creation_fingerprint, lifecycle, queue_json, current_index,
                track_id, state, position_ms, queue_revision, control_version,
                version, epoch, closed_at, created_at, updated_at
            ) VALUES (
                '%s', 'context-closed', 'alice', 'player-1', 'device-1',
                'player-1', 'timeline-closed', 'fingerprint-closed', 'closed',
                '[]', 0, NULL, 'idle', 0, 5, 13, 14, 3,
                '2026-08-07 00:02:00', '2026-08-07 00:00:00',
                '2026-08-07 00:02:00'
            )
            """ % self._legacy_id(provider, 5),
            """
            INSERT INTO emo_playback_control_transaction (
                id, playback_context_id, user_name, epoch,
                command_control_version, requesting_client_id,
                authority_client_id, authority_device_session_id,
                routed_connection_nonce, action, accepted_target_json,
                status, accepted_at_ms, execution_timeout_ms,
                watchdog_deadline_at_ms, created_at, updated_at
            ) VALUES (
                '%s', 'context-active', 'alice', 2, 10, 'controller-1',
                'player-1', 'device-1', 'routed-nonce-1', 'player.play',
                '{"state":"playing"}', 'pending', 1780000001000, 15000,
                1780000018000, '2026-08-07 00:03:00', '2026-08-07 00:03:00'
            )
            """ % root_id,
            """
            INSERT INTO emo_playback_control_transaction (
                id, playback_context_id, user_name, epoch,
                command_control_version, requesting_client_id,
                authority_client_id, authority_device_session_id,
                routed_connection_nonce, action, accepted_target_json,
                status, error_code, depends_on_control_version,
                accepted_at_ms, execution_timeout_ms,
                watchdog_deadline_at_ms, created_at, updated_at
            ) VALUES (
                '%s', 'context-active', 'alice', 2, 11, 'controller-1',
                'player-1', 'device-1', 'routed-nonce-1', 'player.pause',
                '{"state":"paused"}', 'pending', NULL, 10,
                1780000002000, 15000, 1780000019000,
                '2026-08-07 00:04:00', '2026-08-07 00:04:00'
            )
            """ % dependent_id,
            """
            INSERT INTO emo_playback_control_transaction (
                id, playback_context_id, user_name, epoch,
                command_control_version, requesting_client_id,
                authority_client_id, authority_device_session_id,
                routed_connection_nonce, action, accepted_target_json,
                status, error_code, accepted_at_ms, execution_timeout_ms,
                watchdog_deadline_at_ms, applied_control_version,
                terminal_fingerprint, terminal_at_ms, created_at, updated_at
            ) VALUES (
                '%s', 'context-active', 'alice', 2, 12, 'controller-2',
                'player-1', 'device-1', 'routed-nonce-2', 'player.seek',
                '{"positionMs":1000}', 'committed', NULL,
                1780000003000, 15000, 1780000020000, 12,
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                1780000009000, '2026-08-07 00:05:00', '2026-08-07 00:05:00'
            )
            """ % terminal_id,
        ]
        for statement in statements:
            database.execute_sql(statement)

    def _assert_20260728_upgrade_rows(self) -> None:
        context_rows = db.db.execute_sql(
            "SELECT playback_context_id, lifecycle, epoch, version, "
            "queue_revision, control_version, closed_at, "
            "%s FROM emo_playback_context ORDER BY playback_context_id"
            % ", ".join(self.CLOSE_FIELDS)
        ).fetchall()
        self.assertEqual(len(context_rows), 2)
        for row in context_rows:
            self.assertTrue(all(value is None for value in row[7:]))
        self.assertEqual(context_rows[0][0], "context-active")
        self.assertEqual(context_rows[0][1:6], ("active", 2, 12, 4, 10))
        self.assertIsNone(context_rows[0][6])
        self.assertEqual(context_rows[1][0], "context-closed")
        self.assertEqual(context_rows[1][1:6], ("closed", 3, 14, 5, 13))
        self.assertIsNotNone(context_rows[1][6])

        control_rows = db.db.execute_sql(
            "SELECT command_control_version, status, "
            "depends_on_control_version, requesting_device_session_id, "
            "requesting_connection_nonce, requesting_connection_epoch, "
            "effective_at_server_ms, execution_eligible_at_ms, error_message, "
            "reconciled_by_control_version, accepted_at_ms, "
            "watchdog_deadline_at_ms, terminal_fingerprint, terminal_at_ms "
            "FROM emo_playback_control_transaction "
            "ORDER BY command_control_version"
        ).fetchall()
        self.assertEqual(len(control_rows), 3)
        self.assertEqual(
            tuple(row[0:3] for row in control_rows),
            ((10, "pending", None), (11, "pending", 10), (12, "committed", None)),
        )
        for row in control_rows:
            self.assertEqual(row[3:10], (None, None, None, None, None, None, None))
        self.assertEqual(control_rows[0][10:12], (1780000001000, 1780000018000))
        self.assertEqual(control_rows[1][10:12], (1780000002000, 1780000019000))
        self.assertEqual(control_rows[2][10:12], (1780000003000, 1780000020000))
        self.assertEqual(
            control_rows[2][12:],
            (
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                1780000009000,
            ),
        )
        self.assertEqual(
            db.db.execute_sql(
                "SELECT COUNT(*) FROM emo_playback_control_reconciliation"
            ).fetchone()[0],
            0,
        )

    def _assert_large_epoch_round_trip(self) -> None:
        accepted_at_ms = 2**31 + 1000
        eligible_at_ms = 2**31 + 2000
        watchdog_at_ms = 2**31 + 17000
        terminal_at_ms = 2**31 + 18000
        record = db.EmoPlaybackControlTransaction.create(
            playback_context_id="context-active",
            user_name="alice",
            epoch=2,
            command_control_version=13,
            requesting_client_id="controller-large",
            authority_client_id="player-1",
            authority_device_session_id="device-1",
            routed_connection_nonce="routed-large",
            routed_connection_epoch=1,
            requesting_device_session_id="controller-device",
            requesting_connection_nonce="requester-large",
            requesting_connection_epoch=4,
            action="player.play",
            accepted_target_json='{"state":"playing"}',
            status="failed",
            error_code="execution_unknown",
            accepted_at_ms=accepted_at_ms,
            execution_timeout_ms=15000,
            watchdog_deadline_at_ms=watchdog_at_ms,
            effective_at_server_ms=eligible_at_ms,
            execution_eligible_at_ms=eligible_at_ms,
            terminal_fingerprint="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            terminal_at_ms=terminal_at_ms,
        )
        record = db.EmoPlaybackControlTransaction.get_by_id(record.id)
        self.assertEqual(record.accepted_at_ms, accepted_at_ms)
        self.assertEqual(record.effective_at_server_ms, eligible_at_ms)
        self.assertEqual(record.execution_eligible_at_ms, eligible_at_ms)
        self.assertEqual(record.watchdog_deadline_at_ms, watchdog_at_ms)
        self.assertEqual(record.terminal_at_ms, terminal_at_ms)

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
                CREATE TABLE emo_device_playback_state (
                    id CHAR(36) PRIMARY KEY,
                    playback_context_id VARCHAR(128) NOT NULL,
                    device_session_id VARCHAR(128) NOT NULL,
                    owner_client_id VARCHAR(128) NOT NULL,
                    user_name VARCHAR(64) NOT NULL,
                    state VARCHAR(32) NOT NULL,
                    track_id VARCHAR(128),
                    position_ms INTEGER NOT NULL DEFAULT 0,
                    volume INTEGER,
                    is_authority INTEGER NOT NULL DEFAULT 0,
                    mode VARCHAR(32) NOT NULL DEFAULT 'normal',
                    playback_json TEXT,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE(playback_context_id, owner_client_id)
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
            index_columns = tuple(
                row[2]
                for row in db.db.execute_sql(
                    "PRAGMA index_info('%s')" % self.DISCOVERY_INDEX_NAME
                ).fetchall()
            )
            self.assertEqual(index_columns, self.DISCOVERY_INDEX_COLUMNS)
            device_columns = {
                row[1]
                for row in db.db.execute_sql(
                    "PRAGMA table_info('emo_device_playback_state')"
                ).fetchall()
            }
            self.assertTrue(
                {
                    "context_epoch",
                    "applied_control_version",
                    "client_seq",
                }.issubset(device_columns)
            )
            transaction_tables = {
                row[0]
                for row in db.db.execute_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name IN ("
                    "'emo_playback_control_transaction', "
                    "'emo_playback_control_reconciliation', "
                    "'emo_playback_prepare_transaction', "
                    "'emo_playback_local_intent')"
                ).fetchall()
            }
            self.assertEqual(
                transaction_tables,
                {
                    "emo_playback_control_transaction",
                    "emo_playback_control_reconciliation",
                    "emo_playback_prepare_transaction",
                    "emo_playback_local_intent",
                },
            )
            self._assert_sqlite_core_schema_parity()
            self.assertEqual(db.Meta["schema_version"].value, "20260807")
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
                discovery_migration = (
                    root / "migration" / provider / "20260715.sql"
                ).read_text("utf-8")
                transaction_migration = (
                    root / "migration" / provider / "20260717.sql"
                ).read_text("utf-8")
                broadcast_migration = (
                    root / "migration" / provider / "20260726.sql"
                ).read_text("utf-8")
                feedback_migration = (
                    root / "migration" / provider / "20260727.sql"
                ).read_text("utf-8")
                intent_authority_migration = (
                    root / "migration" / provider / "20260728.sql"
                ).read_text("utf-8")
                phase2a_migration = (
                    root / "migration" / provider / "20260807.sql"
                ).read_text("utf-8")
                for field_name in required_fields:
                    self.assertIn(field_name, base_schema)
                    self.assertIn(field_name, migration)
                for field_name in self.CLOSE_FIELDS:
                    self.assertIn(field_name, base_schema)
                    self.assertIn(field_name, phase2a_migration)
                timestamp_type = "TIMESTAMP" if provider == "postgres" else "DATETIME"
                for sql_name, sql in (
                    ("base schema", base_schema),
                    ("broadcast migration", broadcast_migration),
                ):
                    if provider == "postgres":
                        self.assertNotIn(
                            "DATETIME",
                            self._sql_identifier_tokens(sql),
                            "%s contains an independent DATETIME type token" % sql_name,
                        )
                    for table_name, field_names in self.BROADCAST_TIME_FIELDS.items():
                        table_match = re.search(
                            r"CREATE TABLE IF NOT EXISTS\s+%s\s*\((.*?)\);"
                            % re.escape(table_name),
                            sql,
                            flags=re.DOTALL | re.IGNORECASE,
                        )
                        self.assertIsNotNone(table_match, table_name)
                        table_sql = table_match.group(1)
                        for field_name in field_names:
                            self.assertRegex(
                                table_sql,
                                r"\b%s\s+%s\b"
                                % (re.escape(field_name), timestamp_type),
                            )
                self.assertIn("queue_revision", migration)
                self.assertIn("control_version", migration)
                self.assertIn("version", migration)
                self.assertIn("epoch", migration)
                self.assertIn(self.DISCOVERY_INDEX_NAME, base_schema)
                self.assertIn(
                    self.DISCOVERY_INDEX_NAME,
                    discovery_migration,
                )
                for column_name in self.DISCOVERY_INDEX_COLUMNS:
                    self.assertIn(column_name, discovery_migration)
                for field_name in (
                    "context_epoch",
                    "applied_control_version",
                    "client_seq",
                    "emo_playback_control_transaction",
                    "emo_playback_prepare_transaction",
                    "emo_playback_local_intent",
                    "requesting_client_id",
                    "watchdog_deadline_at_ms",
                    "request_fingerprint",
                    "superseded_through_control_version",
                ):
                    self.assertIn(field_name, base_schema)
                    self.assertIn(field_name, transaction_migration)
                for field_name in self.CONTROL_NEW_FIELDS:
                    self.assertIn(field_name, base_schema)
                    self.assertIn(field_name, phase2a_migration)
                for field_name in (
                    "emo_playback_control_reconciliation",
                    "reconciliation_control_version",
                    "from_applied_control_version",
                    "through_control_version",
                    "trigger_kind",
                    "trigger_command_control_version",
                    "actual_fact_fingerprint",
                    "actual_fact_json",
                    "canonical_update_json",
                    "server_updated_at_ms",
                ):
                    self.assertIn(field_name, base_schema)
                    self.assertIn(field_name, phase2a_migration)
                self.assertIn(self.REQUEST_INDEX, base_schema)
                self.assertIn(self.AUTHORITY_INDEX, base_schema)
                self.assertIn(self.RECONCILIATION_INDEX, base_schema)
                self.assertIn(self.REQUEST_INDEX, phase2a_migration)
                self.assertIn(self.AUTHORITY_INDEX, phase2a_migration)
                self.assertIn(self.RECONCILIATION_INDEX, phase2a_migration)
                for table_name in (
                    "emo_broadcast",
                    "emo_broadcast_intent_outcome",
                    "emo_broadcast_fence",
                    "emo_broadcast_participant",
                    "emo_broadcast_revision",
                    "emo_broadcast_delivery",
                    "emo_broadcast_feedback_settlement",
                    "emo_broadcast_terminal_recovery",
                ):
                    self.assertIn(table_name, base_schema)
                    self.assertIn(table_name, broadcast_migration)
                for model in self.BROADCAST_MODELS:
                    for field in model._meta.sorted_fields:
                        self.assertIn(field.column_name, base_schema)
                        if field.column_name in {
                                "applied_at_server_ms",
                                "failed_error_message",
                                "timed_out_broadcast_revision",
                        }:
                            migration = feedback_migration
                        elif (
                            model is db.EmoBroadcastIntentOutcome
                            and field.column_name
                            in {
                                "authority_client_id",
                                "authority_device_session_id",
                            }
                        ):
                            migration = intent_authority_migration
                        else:
                            migration = broadcast_migration
                        self.assertIn(field.column_name, migration)

    def test_sqlite_broadcast_model_schema_parity(self):
        handle, path = tempfile.mkstemp()
        os.close(handle)
        try:
            db.init_database("sqlite:///" + path)
            for model in self.BROADCAST_MODELS + self.CORE_MODELS:
                with self.subTest(table=model._meta.table_name):
                    columns = {
                        row[1]
                        for row in db.db.execute_sql(
                            "PRAGMA table_info('%s')" % model._meta.table_name
                        ).fetchall()
                    }
                    self.assertEqual(
                        columns,
                        {
                            field.column_name
                            for field in model._meta.sorted_fields
                        },
                    )
            self._assert_sqlite_core_schema_parity()
        finally:
            db.release_database()
            os.remove(path)

    def _run_20260728_non_empty_upgrade(self, provider: str, database_uri: str) -> None:
        database_available = False
        initialized = False
        try:
            if provider == "sqlite":
                handle, path = tempfile.mkstemp()
                os.close(handle)
                database_uri = "sqlite:///" + path
            else:
                self._reset_external_database(provider, database_uri)
            database = connect_database(database_uri)
            database.connect()
            database_available = True
            self._create_20260728_fixture(provider, database)
            database.close()

            db.init_database(database_uri)
            initialized = True
            self.assertEqual(db.Meta["schema_version"].value, "20260807")
            self._assert_20260728_upgrade_rows()
            db.release_database()
            initialized = False

            db.init_database(database_uri)
            initialized = True
            self.assertEqual(db.Meta["schema_version"].value, "20260807")
            self._assert_20260728_upgrade_rows()
            self._assert_large_epoch_round_trip()
        finally:
            if initialized:
                db.release_database()
            if provider == "sqlite":
                os.remove(database_uri[len("sqlite:///"):])
            elif database_available:
                self._reset_external_database(provider, database_uri)

    def test_sqlite_20260728_non_empty_upgrade_preserves_rows(self):
        self._run_20260728_non_empty_upgrade("sqlite", "")

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
