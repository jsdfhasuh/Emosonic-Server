import ast
import importlib
from pathlib import Path
from typing import Iterator
import unittest


class DbLayerContractTestCase(unittest.TestCase):
    def _is_relative_to(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
        except ValueError:
            return False
        return True

    def _iter_imported_modules(self, file_path: Path) -> Iterator[str]:
        tree = ast.parse(
            file_path.read_text(encoding="utf-8"), filename=str(file_path)
        )

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue
                prefix = "." * node.level
                yield prefix + node.module

    def test_db_facade_exports_existing_public_names(self):
        db_module = importlib.import_module("supysonic.db")
        expected_names = [
            "SCHEMA_VERSION",
            "db",
            "now",
            "random",
            "PrimaryKeyField",
            "Meta",
            "PathMixin",
            "Image",
            "Folder",
            "Artist",
            "Album",
            "ReviewTask",
            "AlbumReviewTask",
            "AlbumArtist",
            "Track",
            "TrackArtist",
            "TrackMetadata",
            "TrackMetadataEnrichmentTask",
            "delete_track_metadata_for_tracks",
            "User",
            "User_Play_Activity",
            "UserRecommendationFeedback",
            "RecommendationAgentSession",
            "RecommendationAgentCache",
            "ClientPrefs",
            "MusicRequest",
            "EmoSessionQueue",
            "EmoLocalQueue",
            "EmoPlaybackState",
            "EmoPlaybackContext",
            "EmoDevicePlaybackState",
            "EmoPlaybackControlTransaction",
            "EmoPlaybackPrepareTransaction",
            "EmoPlaybackLocalIntent",
            "EmoPlaybackHandoff",
            "StarredFolder",
            "StarredArtist",
            "StarredAlbum",
            "StarredTrack",
            "RatingFolder",
            "RatingTrack",
            "ChatMessage",
            "Playlist",
            "SharedTrackLink",
            "RadioStation",
            "ClientRelease",
            "execute_sql_resource_script",
            "init_database",
            "release_database",
            "open_connection",
            "close_connection",
        ]

        for name in expected_names:
            with self.subTest(name=name):
                self.assertTrue(hasattr(db_module, name))

    def test_db_layer_package_exists(self):
        package = importlib.import_module("supysonic.db_layer")

        self.assertIsNotNone(package)

    def test_core_exports_are_shared_with_facade(self):
        db_module = importlib.import_module("supysonic.db")
        core = importlib.import_module("supysonic.db_layer.core")

        self.assertIs(db_module.db, core.db)
        self.assertIs(db_module.Meta, core.Meta)
        self.assertIs(db_module.PathMixin, core.PathMixin)
        self.assertIs(db_module.now, core.now)
        self.assertIs(db_module.random, core.random)
        self.assertIs(db_module.PrimaryKeyField, core.PrimaryKeyField)

    def test_schema_exports_are_shared_with_facade(self):
        db_module = importlib.import_module("supysonic.db")
        schema = importlib.import_module("supysonic.db_layer.schema")

        self.assertEqual(db_module.SCHEMA_VERSION, schema.SCHEMA_VERSION)
        self.assertIs(db_module.execute_sql_resource_script, schema.execute_sql_resource_script)

    def test_runtime_exports_are_shared_with_facade(self):
        db_module = importlib.import_module("supysonic.db")
        runtime = importlib.import_module("supysonic.db_layer.runtime")

        self.assertIs(db_module.init_database, runtime.init_database)
        self.assertIs(db_module.release_database, runtime.release_database)
        self.assertIs(db_module.open_connection, runtime.open_connection)
        self.assertIs(db_module.close_connection, runtime.close_connection)

    def test_serializer_module_exists(self):
        serializers = importlib.import_module("supysonic.db_layer.serializers")

        for name in (
            "serialize_folder_child",
            "serialize_folder_artist",
            "serialize_folder_directory",
            "serialize_artist",
            "serialize_album",
            "serialize_track_child",
            "serialize_user",
            "serialize_chat_message",
            "serialize_playlist",
            "serialize_radio_station",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(serializers, name))

    def test_db_layer_modules_do_not_import_facade(self):
        package_dir = (
            Path(importlib.import_module("supysonic.db_layer").__file__)
            .resolve()
            .parent
        )

        for path in sorted(package_dir.glob("*.py")):
            if path.name == "__init__.py":
                continue

            for module_name in self._iter_imported_modules(path):
                with self.subTest(path=path.name, module=module_name):
                    self.assertNotEqual(module_name, "supysonic.db")

    def test_non_db_modules_do_not_import_db_layer_directly(self):
        package_dir = (
            Path(importlib.import_module("supysonic").__file__).resolve().parent
        )
        db_layer_dir = package_dir / "db_layer"
        allowed_file = package_dir / "db.py"

        for path in sorted(package_dir.rglob("*.py")):
            if path == allowed_file or self._is_relative_to(path, db_layer_dir):
                continue

            for module_name in self._iter_imported_modules(path):
                with self.subTest(
                    path=str(path.relative_to(package_dir)), module=module_name
                ):
                    self.assertFalse(module_name.startswith("supysonic.db_layer"))
                    self.assertFalse(module_name.startswith(".db_layer"))
                    self.assertFalse(module_name.startswith("..db_layer"))

    def test_low_dependency_models_are_shared_with_facade(self):
        db_module = importlib.import_module("supysonic.db")
        emo = importlib.import_module("supysonic.db_layer.emo")
        client_releases = importlib.import_module(
            "supysonic.db_layer.client_releases"
        )

        self.assertIs(db_module.EmoSessionQueue, emo.EmoSessionQueue)
        self.assertIs(db_module.EmoLocalQueue, emo.EmoLocalQueue)
        self.assertIs(db_module.EmoPlaybackState, emo.EmoPlaybackState)
        self.assertIs(db_module.EmoPlaybackContext, emo.EmoPlaybackContext)
        self.assertIs(
            db_module.EmoDevicePlaybackState,
            emo.EmoDevicePlaybackState,
        )
        self.assertIs(
            db_module.EmoPlaybackControlTransaction,
            emo.EmoPlaybackControlTransaction,
        )
        self.assertIs(
            db_module.EmoPlaybackPrepareTransaction,
            emo.EmoPlaybackPrepareTransaction,
        )
        self.assertIs(
            db_module.EmoPlaybackLocalIntent,
            emo.EmoPlaybackLocalIntent,
        )
        self.assertIs(db_module.EmoPlaybackHandoff, emo.EmoPlaybackHandoff)
        self.assertIs(db_module.ClientRelease, client_releases.ClientRelease)

    def test_main_model_groups_are_shared_with_facade(self):
        db_module = importlib.import_module("supysonic.db")
        library = importlib.import_module("supysonic.db_layer.library")
        users = importlib.import_module("supysonic.db_layer.users")
        annotations = importlib.import_module("supysonic.db_layer.annotations")
        review_tasks = importlib.import_module("supysonic.db_layer.review_tasks")
        track_metadata = importlib.import_module("supysonic.db_layer.track_metadata")
        music_requests = importlib.import_module("supysonic.db_layer.music_requests")
        playlists = importlib.import_module("supysonic.db_layer.playlists")
        misc = importlib.import_module("supysonic.db_layer.misc")

        for name in (
            "Image",
            "Folder",
            "Artist",
            "Album",
            "AlbumArtist",
            "Track",
            "TrackArtist",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(db_module, name), getattr(library, name))

        for name in (
            "User",
            "User_Play_Activity",
            "UserRecommendationFeedback",
            "RecommendationAgentSession",
            "RecommendationAgentCache",
            "ClientPrefs",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(db_module, name), getattr(users, name))

        for name in (
            "StarredFolder",
            "StarredArtist",
            "StarredAlbum",
            "StarredTrack",
            "RatingFolder",
            "RatingTrack",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(db_module, name), getattr(annotations, name))

        self.assertIs(db_module.ReviewTask, review_tasks.ReviewTask)
        self.assertIs(db_module.AlbumReviewTask, review_tasks.AlbumReviewTask)
        self.assertIs(review_tasks.AlbumReviewTask, review_tasks.ReviewTask)
        self.assertIs(db_module.TrackMetadata, track_metadata.TrackMetadata)
        self.assertIs(
            db_module.TrackMetadataEnrichmentTask,
            track_metadata.TrackMetadataEnrichmentTask,
        )
        self.assertIs(
            db_module.delete_track_metadata_for_tracks,
            track_metadata.delete_track_metadata_for_tracks,
        )
        self.assertIs(db_module.MusicRequest, music_requests.MusicRequest)
        self.assertIs(db_module.Playlist, playlists.Playlist)
        self.assertIs(db_module.SharedTrackLink, playlists.SharedTrackLink)
        self.assertIs(db_module.ChatMessage, misc.ChatMessage)
        self.assertIs(db_module.RadioStation, misc.RadioStation)

    def test_mysql_migrations_avoid_unsupported_create_index_if_not_exists(self):
        package_dir = (
            Path(importlib.import_module("supysonic").__file__).resolve().parent
        )
        migration_dir = package_dir / "schema" / "migration" / "mysql"

        for path in sorted(migration_dir.glob("*.sql")):
            sql = path.read_text(encoding="utf-8").upper()
            with self.subTest(path=path.name):
                self.assertNotIn("CREATE INDEX IF NOT EXISTS", sql)
                self.assertNotIn("CREATE UNIQUE INDEX IF NOT EXISTS", sql)


if __name__ == "__main__":
    unittest.main()
