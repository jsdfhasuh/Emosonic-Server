import os
import tempfile
import unittest
from datetime import timedelta
from uuid import uuid4

from flask import current_app

from supysonic.db import (
    Album,
    AlbumArtist,
    AlbumReviewTask,
    Artist,
    Folder,
    ReviewTask,
    Track,
    TrackArtist,
    TrackMetadata,
    User,
    now,
)
from supysonic.tool import write_dict_to_json

from .frontendtestbase import FrontendTestBase
from ..testbase import TestConfig


class MetadataReviewWorkspaceTestCase(FrontendTestBase):
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

        self.artist = Artist.create(name="Review Artist")
        imagePath = os.path.join(tempfile.gettempdir(), "review-artist-image.png")
        write_dict_to_json(
            {"biography": "", "image": {"large": imagePath}},
            imagePath + ".json",
        )
        self.artist.artist_info_json = imagePath + ".json"
        self.artist.save()
        self.album = Album.create(name="Review Album", artist=self.artist, year="2024")
        self.root = Folder.create(root=True, name="Root", path="/tmp/root")
        self.folder = Folder.create(
            root=False,
            name="Album",
            path="/tmp/root/album",
            parent=self.root,
        )
        self.track = Track.create(
            disc=1,
            number=1,
            title="First Track",
            duration=120,
            has_art=False,
            album=self.album,
            artist=self.artist,
            bitrate=320,
            path="/tmp/review-track.flac",
            last_modification=1,
            root_folder=self.root,
            folder=self.folder,
        )
        AlbumArtist.create(album_id=self.album, artist_id=self.artist, position=1)
        TrackArtist.create(track_id=self.track, artist_id=self.artist, position=1)
        self.task = AlbumReviewTask.create(
            album=self.album,
            task_type="metadata_review",
            status="pending",
            reason="new_album",
            snapshot_json='{"album_name": "Review Album", "artist_name": "Review Artist", "track_count": 1}',
        )

    def test_review_workspace_renders_scoped_album_task(self):
        rv = self.client.get(f"/metadata/review-tasks/{self.task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Metadata / Inbox / Review Task", rv.data)
        self.assertIn("Review Album", rv.data)
        self.assertIn("Review Artist", rv.data)
        self.assertIn("Album", rv.data)
        self.assertIn("Tracks", rv.data)
        self.assertIn("Artists", rv.data)
        self.assertIn("Confirm Task", rv.data)
        self.assertIn("Dismiss Task", rv.data)
        self.assertIn("edits are scoped to this album review task", rv.data.lower())
        self.assertIn("metadata-review-feedback", rv.data)
        self.assertIn("data-review-task-action", rv.data)
        self.assertIn("applyResolvedReviewTaskState", rv.data)
        self.assertIn("updateReviewSummaryStatus", rv.data)
        self.assertIn("applyAlbumFormChanges", rv.data)
        self.assertIn("applyTrackFormChanges", rv.data)
        self.assertIn("applyArtistFormChanges", rv.data)
        self.assertIn("review-album-artist-suggestions", rv.data)
        self.assertIn("data-track-artist-suggestions", rv.data)
        self.assertIn("artist-autocomplete.js", rv.data)
        self.assertIn("/metadata/artist-suggestions", rv.data)
        self.assertNotIn("reloadReviewPage('album')", rv.data)
        self.assertNotIn("reloadReviewPage('tracks')", rv.data)
        self.assertIn("reloadReviewPage('artists')", rv.data)
        self.assertIn(f"/rest/getCoverArt?id=al-{self.album.id}&amp;v=1.15.0&amp;c=web", rv.data)
        self.assertIn(f"/rest/getCoverArt?id=ar-{self.artist.id}&amp;v=1.15.0&amp;c=web", rv.data)
        self.assertNotIn("Loading album data...", rv.data)
        self.assertNotIn("Loading artist data...", rv.data)
        self.assertIn("Expires", rv.data)
        self.assertIn("No expiry", rv.data)
        self.assertIn("Issue status", rv.data)
        self.assertIn("Waiting for changes", rv.data)
        self.assertIn("Ready to confirm", rv.data)
        self.assertIn("data-review-issue-status-list", rv.data)
        self.assertIn("data-review-ready-to-confirm", rv.data)
        self.assertIn("evaluateAlbumIssueStatus", rv.data)
        self.assertIn("renderIssueStatus", rv.data)
        self.assertIn("replaceArtistsPanel", rv.data)
        self.assertIn("artistsPanelHtml", rv.data)
        self.assertIn("bindArtistReviewForms", rv.data)

    def test_review_workspace_shows_album_task_expiry_when_present(self):
        self.task.expires_at = now() + timedelta(days=2)
        self.task.save()

        rv = self.client.get(f"/metadata/review-tasks/{self.task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn(self.task.expires_at.strftime('%Y-%m-%d %H:%M:%S'), rv.data)

    def test_review_workspace_includes_back_to_metadata_button(self):
        rv = self.client.get(f"/metadata/review-tasks/{self.task.id}?section=tracks")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Back to Metadata Inbox", rv.data)
        self.assertIn('href="/metadata?tab=inbox"', rv.data)
        self.assertIn("metadata-review-return-bar", rv.data)
        self.assertIn("metadata-review-back-link", rv.data)

    def test_review_workspace_confirm_button_includes_nfo_write_warning(self):
        rv = self.client.get(f"/metadata/review-tasks/{self.task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Confirm Task", rv.data)
        self.assertIn("album.nfo", rv.data)
        self.assertIn("window.confirm", rv.data)
        self.assertIn("button.dataset.reviewTaskLabel === 'confirm'", rv.data)
        self.assertIn("window.location.assign(metadataInboxUrl)", rv.data)
        self.assertIn('/metadata?tab=inbox', rv.data)

    def test_review_workspace_includes_guest_relation_artists(self):
        guest_artist = Artist.create(name="Guest Relation Artist")
        AlbumArtist.create(album_id=self.album, artist_id=guest_artist, position=2)
        TrackArtist.create(track_id=self.album.tracks.get(), artist_id=guest_artist, position=2)

        rv = self.client.get(f"/metadata/review-tasks/{self.task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Guest Relation Artist", rv.data)

    def test_review_workspace_shows_remove_button_only_for_guest_artists(self):
        guest_artist = Artist.create(name="Guest Relation Artist")
        AlbumArtist.create(album_id=self.album, artist_id=guest_artist, position=2)
        TrackArtist.create(track_id=self.album.tracks.get(), artist_id=guest_artist, position=2)

        rv = self.client.get(f"/metadata/review-tasks/{self.task.id}?section=artists")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Remove artist", rv.data)
        self.assertIn(f"/metadata/review-tasks/{self.task.id}/artists/{guest_artist.id}/remove", rv.data)
        self.assertNotIn(f"/metadata/review-tasks/{self.task.id}/artists/{self.artist.id}/remove", rv.data)

    def test_review_workspace_enables_artist_suggestions_for_primary_artist_mapping(self):
        guest_artist = Artist.create(name="Guest Relation Artist")
        AlbumArtist.create(album_id=self.album, artist_id=guest_artist, position=2)
        TrackArtist.create(track_id=self.album.tracks.get(), artist_id=guest_artist, position=2)

        rv = self.client.get(f"/metadata/review-tasks/{self.task.id}?section=artists")

        self.assertEqual(rv.status_code, 200)
        self.assertIn('data-review-primary-artist-suggestions', rv.data)
        self.assertIn("input[name=\"primary_name\"]", rv.data)
        self.assertIn("primaryArtistSuggestionsNode", rv.data)
        self.assertIn("setupArtistAutocomplete(primaryNameField", rv.data)

    def test_review_workspace_handles_artist_metadata_without_image_key(self):
        image_less_artist = Artist.create(name="Image Less Artist")
        image_less_artist.artist_info_json = os.path.join(tempfile.gettempdir(), "image-less-artist.json")
        write_dict_to_json({"biography": "No image key"}, image_less_artist.artist_info_json)
        image_less_artist.save()
        AlbumArtist.create(album_id=self.album, artist_id=image_less_artist, position=2)

        rv = self.client.get(f"/metadata/review-tasks/{self.task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Image Less Artist", rv.data)

    def test_review_workspace_rejects_invalid_task_id(self):
        rv = self.client.get("/metadata/review-tasks/not-a-uuid")

        self.assertEqual(rv.status_code, 400)
        self.assertEqual(rv.json["status"], "error")

    def test_review_workspace_returns_404_for_missing_task(self):
        rv = self.client.get(f"/metadata/review-tasks/{uuid4()}")

        self.assertEqual(rv.status_code, 404)
        self.assertEqual(rv.json["status"], "error")

    def test_review_workspace_honors_requested_section(self):
        rv = self.client.get(f"/metadata/review-tasks/{self.task.id}?section=tracks")

        self.assertEqual(rv.status_code, 200)
        self.assertIn('data-review-panel="tracks"', rv.data)
        self.assertIn("tab-btn active", rv.data)
        self.assertIn("data-review-track-workbench", rv.data)
        self.assertIn("data-review-track-context", rv.data)
        self.assertIn("data-review-track-studio", rv.data)
        self.assertIn("data-review-track-sequence", rv.data)
        self.assertIn("data-review-track-sequence-item", rv.data)
        self.assertIn("data-review-track-editor-panel", rv.data)
        self.assertIn("data-review-track-album-note", rv.data)

    def test_review_workspace_renders_resolved_task_as_read_only(self):
        self.task.status = "confirmed"
        self.task.save()

        rv = self.client.get(f"/metadata/review-tasks/{self.task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("This review task is resolved and now read-only", rv.data)
        self.assertIn("disabled", rv.data)
        self.assertNotIn("Save album changes</button>", rv.data)
        self.assertIn("Reopen Task", rv.data)
        self.assertIn(f"/metadata/review-tasks/{self.task.id}/reopen?redirect=1&amp;section=album", rv.data)

    def test_review_workspace_hides_reopen_button_for_pending_task(self):
        rv = self.client.get(f"/metadata/review-tasks/{self.task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertNotIn("Reopen Task", rv.data)

    def test_review_workspace_renders_artist_review_task(self):
        artist_task = ReviewTask.create(
            entity_type="artist",
            entity_id=str(self.artist.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_image",
            snapshot_json='{"artist_name": "Review Artist", "issues": ["missing_image"]}',
        )

        rv = self.client.get(f"/metadata/review-tasks/{artist_task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Review Artist", rv.data)
        self.assertIn("missing image", rv.data.lower())
        self.assertNotIn("Tracks", rv.data)
        self.assertIn(f"/rest/getCoverArt?id=ar-{self.artist.id}&amp;v=1.15.0&amp;c=web", rv.data)
        self.assertIn('name="artist_photo"', rv.data)
        self.assertIn('accept="image/*"', rv.data)
        self.assertIn("data-review-artist-image-preview", rv.data)
        self.assertIn("URL.createObjectURL", rv.data)
        self.assertIn("new FormData(artistForm)", rv.data)

    def test_review_workspace_renders_track_review_task(self):
        from supysonic.scanner_func.scanner_review_tasks import buildTrackReviewSnapshot

        metadata = TrackMetadata.create(
            track=self.track,
            track_last_modification=self.track.last_modification,
            confidence=0.2,
            provider="local",
            summary="Low confidence semantic summary.",
        )
        metadata.set_tags(["dream pop"])
        metadata.save()
        track_task = ReviewTask.create(
            entity_type="track",
            entity_id=str(self.track.id),
            task_type="metadata_review",
            status="pending",
            reason="low_confidence",
            snapshot_json=buildTrackReviewSnapshot(self.track, metadata),
        )

        rv = self.client.get(f"/metadata/review-tasks/{track_task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Track Review Task", rv.data)
        self.assertIn("First Track", rv.data)
        self.assertIn("Low metadata confidence", rv.data)
        self.assertIn("Low confidence semantic summary.", rv.data)
        self.assertIn("dream pop", rv.data)
        self.assertIn("0.20", rv.data)
        self.assertIn("Confirm Task", rv.data)
        self.assertIn("Dismiss Task", rv.data)
        self.assertIn(f"/rest/getCoverArt?id=al-{self.album.id}&amp;v=1.15.0&amp;c=web", rv.data)

    def test_artist_review_workspace_enables_artist_suggestions_for_primary_artist_mapping(self):
        artist_task = ReviewTask.create(
            entity_type="artist",
            entity_id=str(self.artist.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_image",
            snapshot_json='{"artist_name": "Review Artist", "issues": ["missing_image"]}',
        )

        rv = self.client.get(f"/metadata/review-tasks/{artist_task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn('data-review-primary-artist-suggestions', rv.data)
        self.assertIn("primaryArtistSuggestionsNode", rv.data)
        self.assertIn("setupArtistAutocomplete(primaryNameField", rv.data)
        self.assertIn("Expires", rv.data)
        self.assertIn("No expiry", rv.data)
        self.assertIn("Issue status", rv.data)
        self.assertIn("Waiting for changes", rv.data)
        self.assertIn("Ready to confirm", rv.data)
        self.assertIn("data-review-issue-status-list", rv.data)
        self.assertIn("data-review-ready-to-confirm", rv.data)
        self.assertIn("evaluateArtistIssueStatus", rv.data)
        self.assertIn("renderIssueStatus", rv.data)

    def test_artist_review_workspace_shows_expiry_when_present(self):
        artist_task = ReviewTask.create(
            entity_type="artist",
            entity_id=str(self.artist.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_image",
            snapshot_json='{"artist_name": "Review Artist", "issues": ["missing_image"]}',
            expires_at=now() + timedelta(days=3),
        )

        rv = self.client.get(f"/metadata/review-tasks/{artist_task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn(artist_task.expires_at.strftime('%Y-%m-%d %H:%M:%S'), rv.data)

    def test_artist_review_workspace_shows_reopen_button_for_resolved_task(self):
        artist_task = ReviewTask.create(
            entity_type="artist",
            entity_id=str(self.artist.id),
            task_type="metadata_review",
            status="dismissed",
            reason="missing_image",
            snapshot_json='{"artist_name": "Review Artist", "issues": ["missing_image"]}',
        )

        rv = self.client.get(f"/metadata/review-tasks/{artist_task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Reopen Task", rv.data)
        self.assertIn(f"/metadata/review-tasks/{artist_task.id}/reopen?redirect=1&amp;section=artists", rv.data)

    def test_review_workspace_shows_album_cover_fallback_notice_for_artist_task(self):
        fallback_artist = Artist.create(name="Fallback Artist")
        Album.create(name="Fallback Album", artist=fallback_artist, year="2024")
        artist_task = ReviewTask.create(
            entity_type="artist",
            entity_id=str(fallback_artist.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_image",
            snapshot_json='{"artist_name": "Fallback Artist", "issues": ["missing_image"]}',
        )

        rv = self.client.get(f"/metadata/review-tasks/{artist_task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertIn("Showing album cover fallback", rv.data)
        self.assertIn("Artist image is still missing", rv.data)

    def test_review_workspace_hides_fallback_notice_when_artist_has_dedicated_image(self):
        artist_task = ReviewTask.create(
            entity_type="artist",
            entity_id=str(self.artist.id),
            task_type="metadata_review",
            status="pending",
            reason="missing_image",
            snapshot_json='{"artist_name": "Review Artist", "issues": ["missing_image"]}',
        )

        rv = self.client.get(f"/metadata/review-tasks/{artist_task.id}")

        self.assertEqual(rv.status_code, 200)
        self.assertNotIn("Showing album cover fallback", rv.data)


if __name__ == "__main__":
    unittest.main()
