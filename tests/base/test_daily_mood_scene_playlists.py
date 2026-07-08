import json
import os
import unittest

from supysonic.db import (
    Album,
    Artist,
    Folder,
    Playlist,
    Track,
    TrackMetadata,
    User,
    User_Play_Activity,
)
from supysonic.mood_scene_playlist_service import (
    MOOD_SCENE_PLAYLIST_COMMENT_PREFIX,
    SAVED_MOOD_SCENE_PLAYLIST_COMMENT_PREFIX,
    cleanup_old_mood_scene_playlists,
    create_or_update_daily_mood_scene_playlist_for_user,
    get_mood_scene_playlist_comment,
    is_system_mood_scene_playlist,
    refresh_daily_mood_scene_playlists,
    save_mood_scene_playlist_copy_for_user,
)
from supysonic.recommend import RECOMMENDED_PLAYLIST_COMMENT

from ..testbase import TestBase


class DailyMoodScenePlaylistsTestCase(TestBase):
    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.other_user = User.get(User.name == "bob")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Mood Artist")
        self.album = Album.create(name="Mood Album", artist=self.artist)
        self.track_number = 0

    def _create_track(self, title, genre="jazz", play_count=0):
        self.track_number += 1
        return Track.create(
            disc=1,
            number=self.track_number,
            title=title,
            duration=180,
            has_art=False,
            album=self.album,
            artist=self.artist,
            genre=genre,
            bitrate=320,
            play_count=play_count,
            path=os.path.join("/music", f"{self.track_number}.flac"),
            last_modification=1,
            root_folder=self.root,
            folder=self.root,
        )

    def _metadata(
        self,
        track,
        *,
        mood=None,
        scene=None,
        tags=None,
        language="en",
        energy=40,
        valence=50,
        danceability=40,
        confidence=0.9,
        provider="llm",
        source="llm",
    ):
        return TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            mood_json=json.dumps(mood, ensure_ascii=False) if mood else None,
            scene_json=json.dumps(scene, ensure_ascii=False) if scene else None,
            tags_json=json.dumps(tags, ensure_ascii=False) if tags else None,
            language=language,
            energy=energy,
            valence=valence,
            danceability=danceability,
            confidence=confidence,
            provider=provider,
            source=source,
        )

    def _system_playlist(self, scene_key, day, *tracks):
        playlist = Playlist.create(
            name=f"{self.user.name}'s {day} {scene_key} mood playlist",
            user=self.user,
            comment=get_mood_scene_playlist_comment(scene_key, day),
        )
        for track in tracks:
            playlist.add(track)
        playlist.save()
        return playlist

    def test_create_update_and_second_day_daily_playlist(self):
        night = self._create_track("Night Song")
        self._metadata(
            night,
            mood=["感伤"],
            scene=["深夜"],
            tags=["梦幻"],
            energy=25,
        )

        created = create_or_update_daily_mood_scene_playlist_for_user(
            self.user,
            "night",
            limit=5,
            day="2026-07-07",
        )
        updated = create_or_update_daily_mood_scene_playlist_for_user(
            self.user,
            "night",
            limit=5,
            day="2026-07-07",
        )
        next_day = create_or_update_daily_mood_scene_playlist_for_user(
            self.user,
            "night",
            limit=5,
            day="2026-07-08",
        )

        self.assertEqual(created["status"], "created")
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(next_day["status"], "created")
        self.assertEqual(created["playlist"].id, updated["playlist"].id)
        self.assertNotEqual(created["playlist"].id, next_day["playlist"].id)
        self.assertEqual(updated["playlist"].get_tracks(), [night])
        self.assertEqual(
            Playlist.select()
            .where(
                Playlist.user == self.user,
                Playlist.comment == "mood_scene_playlist:night:2026-07-07",
            )
            .count(),
            1,
        )

    def test_empty_or_unknown_scene_skips_without_playlist(self):
        unknown = create_or_update_daily_mood_scene_playlist_for_user(
            self.user,
            "unknown",
            day="2026-07-07",
        )
        empty = create_or_update_daily_mood_scene_playlist_for_user(
            self.user,
            "night",
            day="2026-07-07",
        )

        self.assertEqual(unknown["status"], "skipped")
        self.assertEqual(unknown["error"], "unknown_scene_key")
        self.assertEqual(empty["status"], "skipped")
        self.assertEqual(Playlist.select().count(), 0)

    def test_create_update_dedupes_existing_system_playlist_duplicates(self):
        night = self._create_track("Night Deduped")
        self._metadata(
            night,
            mood=["感伤"],
            scene=["深夜"],
            tags=["梦幻"],
            energy=25,
        )
        first = self._system_playlist("night", "2026-07-07")
        duplicate = self._system_playlist("night", "2026-07-07")

        result = create_or_update_daily_mood_scene_playlist_for_user(
            self.user,
            "night",
            limit=5,
            day="2026-07-07",
        )
        remaining = Playlist.get(
            Playlist.user == self.user,
            Playlist.comment == "mood_scene_playlist:night:2026-07-07",
        )

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["playlist"].id, remaining.id)
        self.assertIn(remaining.id, {first.id, duplicate.id})
        self.assertEqual(remaining.get_tracks(), [night])
        self.assertEqual(
            Playlist.select()
            .where(
                Playlist.user == self.user,
                Playlist.comment == "mood_scene_playlist:night:2026-07-07",
            )
            .count(),
            1,
        )

    def test_cleanup_deletes_only_expired_system_mood_playlists(self):
        track = self._create_track("Cleanable")
        old_system = self._system_playlist("night", "2026-07-06", track)
        today_system = self._system_playlist("night", "2026-07-07", track)
        invalid_system = Playlist.create(
            name="Invalid system mood playlist",
            user=self.user,
            comment=f"{MOOD_SCENE_PLAYLIST_COMMENT_PREFIX}night:not-a-day",
        )
        saved_copy = Playlist.create(
            name="Saved mood playlist",
            user=self.user,
            comment=f"{SAVED_MOOD_SCENE_PLAYLIST_COMMENT_PREFIX}night:2026-07-06",
        )
        normal = Playlist.create(name="Normal", user=self.user)
        recommended = Playlist.create(
            name="Recommended",
            user=self.user,
            comment=RECOMMENDED_PLAYLIST_COMMENT,
        )

        result = cleanup_old_mood_scene_playlists(
            retention_days=1,
            current_day="2026-07-07",
        )

        self.assertEqual(result, {"deleted": 1, "skipped": 1})
        self.assertIsNone(Playlist.get_or_none(Playlist.id == old_system.id))
        for playlist in (today_system, invalid_system, saved_copy, normal, recommended):
            self.assertIsNotNone(Playlist.get_or_none(Playlist.id == playlist.id))

    def test_save_copy_preserves_tracks_and_uses_saved_comment_prefix(self):
        first = self._create_track("First")
        second = self._create_track("Second")
        source = self._system_playlist("night", "2026-07-07", first, second)

        saved = save_mood_scene_playlist_copy_for_user(self.user, source)

        self.assertIsNotNone(saved)
        self.assertFalse(is_system_mood_scene_playlist(saved))
        self.assertEqual(saved.comment, "saved_mood_scene_playlist:night:2026-07-07")
        self.assertEqual(saved.get_tracks(), [first, second])

    def test_refresh_daily_mood_scene_playlists_respects_active_user_scope(self):
        night = self._create_track("Night Active")
        self._metadata(
            night,
            mood=["感伤"],
            scene=["深夜"],
            tags=["梦幻"],
            energy=25,
        )
        User_Play_Activity.create(track=night, user=self.user)

        active_only = refresh_daily_mood_scene_playlists(
            limit=1,
            day="2026-07-07",
            active_users_only=True,
        )
        self.other_user.last_play = night
        self.other_user.save()
        with_last_play = refresh_daily_mood_scene_playlists(
            limit=1,
            day="2026-07-08",
            active_users_only=True,
        )

        self.assertEqual(active_only["users"], 1)
        self.assertEqual(with_last_play["users"], 2)
        self.assertGreaterEqual(
            Playlist.select()
            .where(
                Playlist.user == self.user,
                Playlist.comment.startswith(MOOD_SCENE_PLAYLIST_COMMENT_PREFIX),
            )
            .count(),
            1,
        )
        self.assertGreaterEqual(
            Playlist.select()
            .where(
                Playlist.user == self.other_user,
                Playlist.comment.startswith(MOOD_SCENE_PLAYLIST_COMMENT_PREFIX),
            )
            .count(),
            1,
        )


if __name__ == "__main__":
    unittest.main()
