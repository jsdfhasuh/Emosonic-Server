import unittest
from datetime import timedelta

from flask import current_app

from supysonic.db import (
    Album,
    AlbumReviewTask,
    Artist,
    Folder,
    ReviewTask,
    Track,
    TrackMetadata,
    User,
    db,
    now,
)

from .frontendtestbase import FrontendTestBase
from ..testbase import TestConfig


class MetadataInboxTestCase(FrontendTestBase):
    __with_api__ = True

    def setUp(self):
        TestConfig.WEBAPP = TestConfig.WEBAPP.copy()
        TestConfig.WEBAPP["log_dir"] = ""
        super().setUp()

        alice = User.get(User.name == "alice")
        with self.client.session_transaction() as session:
            session["userid"] = str(alice.id)

        with self.app_context():
            view_functions = current_app.view_functions
            if "emo.list_logs" not in view_functions:
                current_app.add_url_rule(
                    "/emo/test-logs",
                    endpoint="emo.list_logs",
                    view_func=lambda: "",
                )

    def _create_track(self, title="Inbox Track"):
        artist = Artist.create(name=f"{title} Artist")
        album = Album.create(name=f"{title} Album", artist=artist, year="2024")
        root = Folder.create(root=True, name=f"{title} Root", path=f"/tmp/{title}/root")
        folder = Folder.create(
            root=False,
            name=f"{title} Folder",
            path=f"/tmp/{title}/root/album",
            parent=root,
        )
        return Track.create(
            disc=1,
            number=1,
            title=title,
            duration=180,
            has_art=False,
            album=album,
            artist=artist,
            bitrate=320,
            path=f"/tmp/{title}/track.flac",
            last_modification=1,
            root_folder=root,
            folder=folder,
        )

    def test_metadata_inbox_renders_pending_review_task(self):
        artist = Artist.create(name="Review Artist")
        album = Album.create(name="Review Album", artist=artist, year="2024")
        task = AlbumReviewTask.create(
            album=album,
            task_type="metadata_review",
            status="pending",
            reason="new_album",
            snapshot_json='{"album_name": "Review Album", "track_count": 0}',
        )
        task.expires_at = now() + timedelta(days=2)
        task.save()

        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Review Album", rv.data)
        self.assertIn("Review", rv.data)
        self.assertIn("Confirm", rv.data)
        self.assertIn("Dismiss", rv.data)
        self.assertIn(f"/rest/getCoverArt?id=al-{album.id}&amp;v=1.15.0&amp;c=web", rv.data)
        self.assertIn("Expires", rv.data)
        self.assertIn(task.expires_at.strftime('%Y-%m-%d %H:%M:%S'), rv.data)
        self.assertNotIn("Expires:", rv.data)
        self.assertNotIn("No pending review tasks yet", rv.data)

    def test_metadata_inbox_keeps_empty_state_without_tasks(self):
        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("No pending review tasks yet", rv.data)

    def test_metadata_inbox_keeps_filters_visible_when_selected_status_has_no_tasks(self):
        artist = Artist.create(name="Confirmed Artist")
        album = Album.create(name="Confirmed Album", artist=artist, year="2024")
        ReviewTask.create(
            entity_type="album",
            entity_id=str(album.id),
            task_type="metadata_review",
            status="confirmed",
            reason="new_album",
            snapshot_json='{"album_name": "Confirmed Album", "artist_name": "Confirmed Artist", "track_count": 1}',
        )

        rv = self.client.get("/metadata?tab=inbox&status=dismissed")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Pending", rv.data)
        self.assertIn("Confirmed", rv.data)
        self.assertIn("Dismissed", rv.data)
        self.assertIn("All", rv.data)
        self.assertIn("No dismissed review tasks yet", rv.data)

    def test_metadata_inbox_renders_pending_artist_review_task(self):
        artist = Artist.create(name="Image Less Artist")
        ReviewTask.create(
            entity_type="artist",
            entity_id=str(artist.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_image",
            snapshot_json='{"artist_name": "Image Less Artist"}',
        )

        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Image Less Artist", rv.data)
        self.assertIn("missing image", rv.data.lower())
        self.assertIn(f"/rest/getCoverArt?id=ar-{artist.id}&amp;v=1.15.0&amp;c=web", rv.data)
        self.assertIn("Expires", rv.data)
        self.assertIn("No expiry", rv.data)
        self.assertNotIn("Expires:", rv.data)

    def test_metadata_inbox_renders_pending_track_review_task(self):
        from supysonic.scanner_func.scanner_review_tasks import buildTrackReviewSnapshot

        track = self._create_track("Low Confidence Track")
        metadata = TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            confidence=0.2,
            provider="local",
        )
        metadata.set_tags(["dream pop"])
        metadata.save()
        task = ReviewTask.create(
            entity_type="track",
            entity_id=str(track.id),
            task_type="metadata_review",
            status="pending",
            reason="low_confidence",
            snapshot_json=buildTrackReviewSnapshot(track, metadata),
        )

        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Track tasks (1)", rv.data)
        self.assertIn("Low Confidence Track", rv.data)
        self.assertIn("Low metadata confidence", rv.data)
        self.assertIn("Confidence: 0.20", rv.data)
        self.assertIn("Tags: dream pop", rv.data)
        self.assertIn(f"/metadata/review-tasks/{task.id}", rv.data)
        self.assertIn(f"/rest/getCoverArt?id=al-{track.album_id}&amp;v=1.15.0&amp;c=web", rv.data)

    def test_metadata_inbox_shows_album_cover_fallback_notice_for_artist_task(self):
        artist = Artist.create(name="Fallback Artist")
        Album.create(name="Fallback Album", artist=artist, year="2024")
        ReviewTask.create(
            entity_type="artist",
            entity_id=str(artist.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_image",
            snapshot_json='{"artist_name": "Fallback Artist"}',
        )

        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Showing album cover fallback", rv.data)

    def test_metadata_inbox_describes_missing_year_album_task(self):
        artist = Artist.create(name="Vaundy")
        album = Album.create(name="RUN SAKAMOTO RUN", artist=artist, year="")
        ReviewTask.create(
            entity_type="album",
            entity_id=str(album.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_year",
            snapshot_json='{"album_name": "RUN SAKAMOTO RUN", "artist_name": "Vaundy", "track_count": 2, "issues": ["missing_year"]}',
        )

        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Album metadata review created at", rv.data)
        self.assertIn("Current issue: Missing release year.", rv.data)
        self.assertNotIn("New album metadata review created at", rv.data)

    def test_metadata_inbox_describes_legacy_missing_year_album_task_without_snapshot_issues(self):
        artist = Artist.create(name="Legacy Artist")
        album = Album.create(name="Legacy Album", artist=artist, year="")
        ReviewTask.create(
            entity_type="album",
            entity_id=str(album.id).replace("-", ""),
            task_type="metadata_review",
            status="pending",
            reason="missing_year",
            snapshot_json='{"album_id": "%s", "album_name": "Legacy Album", "artist_name": "Legacy Artist", "year": null, "track_count": 1}' % album.id,
        )

        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Current issue: Missing release year.", rv.data)

    def test_metadata_inbox_describes_album_task_with_track_artist_issue(self):
        artist = Artist.create(name="Mapped Artist")
        album = Album.create(name="Mapped Album", artist=artist, year="2024")
        ReviewTask.create(
            entity_type="album",
            entity_id=str(album.id),
            task_type="metadata_review",
            status="pending",
            reason="new_album",
            snapshot_json='{"album_name": "Mapped Album", "artist_name": "Mapped Artist", "track_count": 1, "issues": ["track_artist_mapping_needs_review"]}',
        )

        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("New album metadata review created at", rv.data)
        self.assertIn("Current issue: Track artist mapping needs review.", rv.data)

    def test_metadata_inbox_describes_album_task_with_multiple_issues(self):
        artist = Artist.create(name="Complex Artist")
        album = Album.create(name="Complex Album", artist=artist, year="")
        ReviewTask.create(
            entity_type="album",
            entity_id=str(album.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_year",
            snapshot_json='{"album_name": "Complex Album", "artist_name": "Complex Artist", "track_count": 1, "issues": ["missing_year", "track_artist_mapping_needs_review"]}',
        )

        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Album metadata review created at", rv.data)
        self.assertIn("Current issues: Missing release year, Track artist mapping needs review.", rv.data)

    def test_metadata_inbox_uses_generic_album_description_for_unknown_reason(self):
        artist = Artist.create(name="Future Artist")
        album = Album.create(name="Future Album", artist=artist, year="2024")
        ReviewTask.create(
            entity_type="album",
            entity_id=str(album.id),
            task_type="metadata_review",
            status="pending",
            reason="future_reason",
            snapshot_json='{"album_name": "Future Album", "artist_name": "Future Artist", "track_count": 1, "issues": ["future_issue"]}',
        )

        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Album metadata review created at", rv.data)
        self.assertNotIn("New album metadata review created at", rv.data)

    def test_metadata_inbox_describes_artist_task_with_current_issue(self):
        artist = Artist.create(name="Image Less Artist")
        ReviewTask.create(
            entity_type="artist",
            entity_id=str(artist.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_image",
            snapshot_json='{"artist_name": "Image Less Artist", "issues": ["missing_image"]}',
        )

        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Artist metadata review created at", rv.data)
        self.assertIn("Current issue: Missing artist image.", rv.data)

    def test_metadata_inbox_groups_album_tasks_before_artist_tasks(self):
        album_artist = Artist.create(name="Album Artist")
        album = Album.create(name="Album First", artist=album_artist, year="2024")
        artist = Artist.create(name="Artist Second")
        ReviewTask.create(
            entity_type="artist",
            entity_id=str(artist.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_image",
            snapshot_json='{"artist_name": "Artist Second", "issues": ["missing_image"]}',
        )
        ReviewTask.create(
            entity_type="album",
            entity_id=str(album.id),
            task_type="metadata_review",
            status="pending",
            reason="new_album",
            snapshot_json='{"album_name": "Album First", "artist_name": "Album Artist", "track_count": 1}',
        )

        rv = self.client.get("/metadata?tab=inbox")
        body = rv.data

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Album tasks (1)", body)
        self.assertIn("Artist tasks (1)", body)
        self.assertLess(body.index("Album tasks (1)"), body.index("Artist tasks (1)"))
        self.assertLess(body.index("Album First"), body.index("Artist Second"))

    def test_metadata_inbox_hides_superseded_new_artist_task_when_missing_image_exists(self):
        artist = Artist.create(name="Gracie Abrams")
        ReviewTask.create(
            entity_type="artist",
            entity_id=str(artist.id),
            task_type="metadata_review",
            status="pending",
            reason="new_artist",
            snapshot_json='{"artist_name": "Gracie Abrams", "issues": ["missing_image"]}',
        )
        ReviewTask.create(
            entity_type="artist",
            entity_id=str(artist.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_image",
            snapshot_json='{"artist_name": "Gracie Abrams", "issues": ["missing_image"]}',
        )

        rv = self.client.get("/metadata?tab=inbox")
        body = rv.data

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Artist tasks (1)", body)
        self.assertIn("missing image", body.lower())
        self.assertNotIn("new artist", body.lower())

    def test_metadata_inbox_sorts_album_tasks_by_priority(self):
        artist = Artist.create(name="Priority Artist")
        high_album = Album.create(name="High Priority Album", artist=artist, year="")
        medium_album = Album.create(name="Medium Priority Album", artist=artist, year="")
        low_album = Album.create(name="Low Priority Album", artist=artist, year="2024")
        clean_album = Album.create(name="Clean Album", artist=artist, year="2024")
        ReviewTask.create(
            entity_type="album",
            entity_id=str(clean_album.id),
            task_type="metadata_review",
            status="pending",
            reason="new_album",
            snapshot_json='{"album_name": "Clean Album", "artist_name": "Priority Artist", "track_count": 1}',
        )
        ReviewTask.create(
            entity_type="album",
            entity_id=str(low_album.id),
            task_type="metadata_review",
            status="pending",
            reason="new_album",
            snapshot_json='{"album_name": "Low Priority Album", "artist_name": "Priority Artist", "track_count": 1, "issues": ["track_artist_mapping_needs_review"]}',
        )
        ReviewTask.create(
            entity_type="album",
            entity_id=str(medium_album.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_year",
            snapshot_json='{"album_name": "Medium Priority Album", "artist_name": "Priority Artist", "track_count": 1, "issues": ["missing_year"]}',
        )
        ReviewTask.create(
            entity_type="album",
            entity_id=str(high_album.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_year",
            snapshot_json='{"album_name": "High Priority Album", "artist_name": "Priority Artist", "track_count": 1, "issues": ["missing_year", "track_artist_mapping_needs_review"]}',
        )

        rv = self.client.get("/metadata?tab=inbox")
        body = rv.data

        self.assertEqual(rv.status_code, 200)
        self.assertLess(body.index("High Priority Album"), body.index("Medium Priority Album"))
        self.assertLess(body.index("Medium Priority Album"), body.index("Low Priority Album"))
        self.assertLess(body.index("Low Priority Album"), body.index("Clean Album"))

    def test_metadata_inbox_sorts_same_priority_album_tasks_by_oldest_first(self):
        artist = Artist.create(name="Chronology Artist")
        older_album = Album.create(name="Older Album", artist=artist, year="")
        newer_album = Album.create(name="Newer Album", artist=artist, year="")
        older_task = ReviewTask.create(
            entity_type="album",
            entity_id=str(older_album.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_year",
            snapshot_json='{"album_name": "Older Album", "artist_name": "Chronology Artist", "track_count": 1, "issues": ["missing_year"]}',
        )
        newer_task = ReviewTask.create(
            entity_type="album",
            entity_id=str(newer_album.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_year",
            snapshot_json='{"album_name": "Newer Album", "artist_name": "Chronology Artist", "track_count": 1, "issues": ["missing_year"]}',
        )
        older_task.created = now() - timedelta(hours=2)
        older_task.save()
        newer_task.created = now() - timedelta(hours=1)
        newer_task.save()

        rv = self.client.get("/metadata?tab=inbox")
        body = rv.data

        self.assertEqual(rv.status_code, 200)
        self.assertLess(body.index("Older Album"), body.index("Newer Album"))

    def test_metadata_inbox_handles_orphaned_album_review_task(self):
        artist = Artist.create(name="Deleted Album Artist")
        album = Album.create(name="Deleted Album", artist=artist, year="2024")
        task = ReviewTask.create(
            entity_type="album",
            entity_id=str(album.id),
            task_type="metadata_review",
            status="pending",
            reason="new_album",
            snapshot_json='{"album_name": "Deleted Album", "artist_name": "Deleted Album Artist", "track_count": 0}',
        )
        album.delete_instance()

        rv = self.client.get("/metadata?tab=inbox")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Deleted Album", rv.data)
        self.assertIn(f"/metadata/review-tasks/{task.id}", rv.data)


if __name__ == "__main__":
    unittest.main()
