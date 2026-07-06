import json
import os
import uuid
import unittest

from datetime import datetime
from unittest.mock import patch

from supysonic.db import (
    Album,
    Artist,
    Folder,
    Playlist,
    Track,
    TrackMetadata,
    User,
    User_Play_Activity,
    db,
)
from supysonic.recommend import (
    RECOMMENDED_PLAYLIST_COMMENT,
    _buildRecommendedTracks,
    buildRecommendationReasonMap,
    create_recommend_playlist,
)
from supysonic.recommendation_feedback import set_recommendation_feedback

from ..testbase import TestBase


class RecommendTestCase(TestBase):
    def setUp(self):
        super().setUp()
        db.execute_sql(
            """
            CREATE TABLE IF NOT EXISTS user_play_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id CHAR(36) NOT NULL REFERENCES track(id) ON DELETE CASCADE,
                user_id CHAR(36) NOT NULL REFERENCES user(id) ON DELETE CASCADE,
                time DATETIME NOT NULL
            )
            """
        )
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Artist")
        self.album = Album.create(name="Album", artist=self.artist)
        self.listened_tracks = [
            self._create_track("Listened One", 1, genre="rock"),
            self._create_track("Listened Two", 2, genre="rock"),
        ]
        self.candidate_tracks = [
            self._create_track("Candidate Rock", 3, genre="rock"),
            self._create_track("Candidate Artist", 4, genre="pop"),
            self._create_track("Candidate Other", 5, genre="jazz"),
        ]

    def _create_track(
        self,
        title,
        number,
        genre=None,
        artist=None,
        album=None,
        play_count=0,
        track_id=None,
    ):
        artist = artist or self.artist
        album = album or self.album
        values = {
            "disc": 1,
            "number": number,
            "title": title,
            "duration": 180,
            "has_art": False,
            "album": album,
            "artist": artist,
            "genre": genre,
            "bitrate": 320,
            "play_count": play_count,
            "path": os.path.join("/music", f"{number}.flac"),
            "last_modification": 1,
            "root_folder": self.root,
            "folder": self.root,
        }
        if track_id is not None:
            values["id"] = track_id
        return Track.create(**values)

    def _record_play(self, track, count):
        for _ in range(count):
            User_Play_Activity.create(track=track, user=self.user)

    def _create_recommended_playlist(self, day, *tracks):
        playlist = Playlist.create(
            user=self.user,
            name=f"{self.user.name}'s {day} recommend playlist",
            comment=RECOMMENDED_PLAYLIST_COMMENT,
        )
        for track in tracks or self.candidate_tracks[:1]:
            playlist.add(track)
        playlist.save()
        return playlist

    def test_create_recommend_playlist_creates_local_day_playlist_with_recommended_comment(self):
        self._record_play(self.listened_tracks[0], 3)
        self._record_play(self.listened_tracks[1], 1)

        created = create_recommend_playlist(num_songs=3, user=self.user, day="2026-05-02")

        self.assertEqual(created, 1)
        playlist = Playlist.get(Playlist.user == self.user)
        self.assertEqual(playlist.name, "alice's 2026-05-02 recommend playlist")
        self.assertEqual(playlist.comment, RECOMMENDED_PLAYLIST_COMMENT)

    def test_create_recommend_playlist_excludes_already_listened_tracks(self):
        self._record_play(self.listened_tracks[0], 2)
        self._record_play(self.listened_tracks[1], 1)

        create_recommend_playlist(num_songs=3, user=self.user, day="2026-05-02")

        playlist = Playlist.get(Playlist.user == self.user)
        recommended_ids = {track.id for track in playlist.get_tracks()}
        listened_ids = {track.id for track in self.listened_tracks}
        self.assertTrue(recommended_ids)
        self.assertTrue(recommended_ids.isdisjoint(listened_ids))

    def test_create_recommend_playlist_excludes_user_disliked_tracks(self):
        self._record_play(self.listened_tracks[0], 2)
        self._record_play(self.listened_tracks[1], 1)
        disliked_track = self.candidate_tracks[0]

        set_recommendation_feedback(
            self.user,
            str(disliked_track.id),
            "dislike",
        )
        create_recommend_playlist(num_songs=3, user=self.user, day="2026-05-02")

        playlist = Playlist.get(Playlist.user == self.user)
        recommended_ids = {track.id for track in playlist.get_tracks()}
        self.assertNotIn(disliked_track.id, recommended_ids)

    def test_create_recommend_playlist_excludes_hidden_artist_tracks(self):
        self._record_play(self.listened_tracks[0], 2)
        hidden_artist = Artist.create(name="Hidden Artist")
        hidden_album = Album.create(name="Hidden Album", artist=hidden_artist)
        visible_artist = Artist.create(name="Visible Artist")
        visible_album = Album.create(name="Visible Album", artist=visible_artist)
        hidden_track = self._create_track(
            "Hidden Artist Candidate",
            6,
            genre="rock",
            artist=hidden_artist,
            album=hidden_album,
        )
        visible_track = self._create_track(
            "Visible Artist Candidate",
            7,
            genre="rock",
            artist=visible_artist,
            album=visible_album,
        )

        set_recommendation_feedback(
            self.user,
            str(self.artist.id),
            "hide_artist",
        )
        set_recommendation_feedback(
            self.user,
            "hidden artist",
            "hide_artist",
        )
        create_recommend_playlist(num_songs=2, user=self.user, day="2026-05-02")

        playlist = Playlist.get(Playlist.user == self.user)
        recommended_ids = {track.id for track in playlist.get_tracks()}
        self.assertNotIn(hidden_track.id, recommended_ids)
        self.assertIn(visible_track.id, recommended_ids)

    def test_create_recommend_playlist_excludes_hidden_album_and_style_tracks(self):
        self._record_play(self.listened_tracks[0], 2)
        hidden_album_artist = Artist.create(name="Hidden Album Artist")
        hidden_album = Album.create(
            name="Hidden Album",
            artist=hidden_album_artist,
        )
        hidden_style_artist = Artist.create(name="Hidden Style Artist")
        hidden_style_album = Album.create(
            name="Hidden Style Album",
            artist=hidden_style_artist,
        )
        visible_artist = Artist.create(name="Visible Album Style Artist")
        visible_album = Album.create(
            name="Visible Album Style Album",
            artist=visible_artist,
        )
        hidden_album_track = self._create_track(
            "Hidden Album Candidate",
            8,
            genre="rock",
            artist=hidden_album_artist,
            album=hidden_album,
            play_count=100,
        )
        hidden_style_track = self._create_track(
            "Hidden Style Candidate",
            9,
            genre="Blocked",
            artist=hidden_style_artist,
            album=hidden_style_album,
            play_count=90,
        )
        visible_track = self._create_track(
            "Visible Album Style Candidate",
            10,
            genre="rock",
            artist=visible_artist,
            album=visible_album,
            play_count=1,
        )

        set_recommendation_feedback(
            self.user,
            str(hidden_album.id),
            "hide_album",
        )
        set_recommendation_feedback(
            self.user,
            "blocked",
            "not_this_style",
        )
        create_recommend_playlist(num_songs=6, user=self.user, day="2026-05-02")

        playlist = Playlist.get(Playlist.user == self.user)
        recommended_ids = {track.id for track in playlist.get_tracks()}
        self.assertNotIn(hidden_album_track.id, recommended_ids)
        self.assertNotIn(hidden_style_track.id, recommended_ids)
        self.assertIn(visible_track.id, recommended_ids)

    def test_create_recommend_playlist_scores_genre_affinity_above_popularity(self):
        self._record_play(self.listened_tracks[0], 3)
        set_recommendation_feedback(
            self.user,
            str(self.artist.id),
            "hide_artist",
        )
        rock_artist = Artist.create(name="Rock Candidate Artist")
        rock_album = Album.create(name="Rock Candidate Album", artist=rock_artist)
        jazz_artist = Artist.create(name="Popular Jazz Artist")
        jazz_album = Album.create(name="Popular Jazz Album", artist=jazz_artist)
        rock_candidate = self._create_track(
            "Low Play Rock Candidate",
            8,
            genre="rock",
            artist=rock_artist,
            album=rock_album,
            play_count=0,
        )
        jazz_candidate = self._create_track(
            "Popular Jazz Candidate",
            9,
            genre="jazz",
            artist=jazz_artist,
            album=jazz_album,
            play_count=100,
        )

        create_recommend_playlist(num_songs=1, user=self.user, day="2026-05-02")

        playlist = Playlist.get(Playlist.user == self.user)
        recommended_ids = [track.id for track in playlist.get_tracks()]
        self.assertEqual(recommended_ids, [rock_candidate.id])
        self.assertNotIn(jazz_candidate.id, recommended_ids)

    def test_create_recommend_playlist_scores_album_affinity_for_equal_candidates(self):
        self._record_play(self.listened_tracks[0], 3)
        set_recommendation_feedback(
            self.user,
            str(self.artist.id),
            "hide_artist",
        )
        affinity_artist = Artist.create(name="Album Affinity Artist")
        affinity_candidate = self._create_track(
            "Same Album Candidate",
            10,
            genre="ambient",
            artist=affinity_artist,
            album=self.album,
            play_count=7,
        )
        other_artist = Artist.create(name="Different Album Artist")
        other_album = Album.create(
            name="Different Album",
            artist=other_artist,
        )
        other_candidate = self._create_track(
            "Different Album Candidate",
            11,
            genre="ambient",
            artist=other_artist,
            album=other_album,
            play_count=7,
        )

        create_recommend_playlist(num_songs=1, user=self.user, day="2026-05-02")

        playlist = Playlist.get(Playlist.user == self.user)
        recommended_ids = [track.id for track in playlist.get_tracks()]
        self.assertEqual(recommended_ids, [affinity_candidate.id])
        self.assertNotIn(other_candidate.id, recommended_ids)

    def test_create_recommend_playlist_scores_freshness_for_equal_candidates(self):
        self._record_play(self.listened_tracks[0], 3)
        set_recommendation_feedback(
            self.user,
            str(self.artist.id),
            "hide_artist",
        )
        fresh_artist = Artist.create(name="Fresh Candidate Artist")
        fresh_album = Album.create(name="Fresh Candidate Album", artist=fresh_artist)
        stale_artist = Artist.create(name="Stale Candidate Artist")
        stale_album = Album.create(name="Stale Candidate Album", artist=stale_artist)
        fresh_candidate = self._create_track(
            "Fresh Candidate",
            12,
            genre="ambient",
            artist=fresh_artist,
            album=fresh_album,
            play_count=7,
        )
        stale_candidate = self._create_track(
            "Stale Candidate",
            13,
            genre="ambient",
            artist=stale_artist,
            album=stale_album,
            play_count=7,
        )
        stale_candidate.last_play = datetime(2026, 5, 1, 12, 0, 0)
        stale_candidate.save()

        create_recommend_playlist(num_songs=1, user=self.user, day="2026-05-02")

        playlist = Playlist.get(Playlist.user == self.user)
        recommended_ids = [track.id for track in playlist.get_tracks()]
        self.assertEqual(recommended_ids, [fresh_candidate.id])
        self.assertNotIn(stale_candidate.id, recommended_ids)

    def test_build_recommended_tracks_scores_like_more_feedback(self):
        seed_artist = Artist.create(name="Seed Artist")
        seed_album = Album.create(name="Seed Album", artist=seed_artist)
        other_artist = Artist.create(name="Other Artist")
        other_album = Album.create(name="Other Album", artist=other_artist)
        seed_track = self._create_track(
            "Liked Seed",
            10,
            genre="ambient",
            artist=seed_artist,
            album=seed_album,
        )
        similar_track = self._create_track(
            "Similar Candidate",
            11,
            genre="ambient",
            artist=seed_artist,
            album=seed_album,
        )
        other_track = self._create_track(
            "Other Candidate",
            12,
            genre="ambient",
            artist=other_artist,
            album=other_album,
        )

        tracks = _buildRecommendedTracks(
            {self.listened_tracks[0].id: 1, self.listened_tracks[1].id: 1},
            1,
            excludedTrackIds={seed_track.id, *[track.id for track in self.candidate_tracks]},
            preferences={
                "disliked_song_ids": set(),
                "hidden_artist_ids": set(),
                "hidden_album_ids": set(),
                "hidden_genres": set(),
                "liked_more_song_ids": {str(seed_track.id)},
            },
        )

        self.assertEqual([track.id for track in tracks], [similar_track.id])
        self.assertNotEqual(tracks[0].id, other_track.id)

    def test_build_recommended_tracks_scores_track_metadata_affinity(self):
        seed_track = self.listened_tracks[0]
        TrackMetadata.create(
            track=seed_track,
            track_last_modification=seed_track.last_modification,
            mood_json='["calm"]',
            scene_json='["late night"]',
            tags_json='["dreamy"]',
            energy=35,
            provider="test",
        )
        metadata_artist = Artist.create(name="Metadata Candidate Artist")
        metadata_album = Album.create(name="Metadata Candidate Album", artist=metadata_artist)
        other_artist = Artist.create(name="Other Metadata Artist")
        other_album = Album.create(name="Other Metadata Album", artist=other_artist)
        metadata_candidate = self._create_track(
            "Metadata Candidate",
            30,
            genre="ambient",
            artist=metadata_artist,
            album=metadata_album,
        )
        other_candidate = self._create_track(
            "Other Metadata Candidate",
            31,
            genre="ambient",
            artist=other_artist,
            album=other_album,
        )
        TrackMetadata.create(
            track=metadata_candidate,
            track_last_modification=metadata_candidate.last_modification,
            mood_json='["calm"]',
            scene_json='["late night"]',
            tags_json='["dreamy"]',
            energy=40,
            provider="test",
        )
        TrackMetadata.create(
            track=other_candidate,
            track_last_modification=other_candidate.last_modification,
            mood_json='["aggressive"]',
            scene_json='["workout"]',
            tags_json='["harsh"]',
            energy=95,
            provider="test",
        )

        tracks = _buildRecommendedTracks(
            {seed_track.id: 3},
            1,
            excludedTrackIds={
                self.listened_tracks[1].id,
                *[track.id for track in self.candidate_tracks],
            },
        )

        self.assertEqual([track.id for track in tracks], [metadata_candidate.id])
        self.assertNotEqual(tracks[0].id, other_candidate.id)

    def test_build_recommended_tracks_rotates_equal_candidates_by_day(self):
        existing_ids = {
            track.id
            for track in self.listened_tracks + self.candidate_tracks
        }
        tied_tracks = [
            self._create_track(
                f"Tied Candidate {index}",
                20 + index,
                genre="ambient",
                play_count=0,
                track_id=uuid.UUID(
                    f"00000000-0000-0000-0000-00000000000{index}"
                ),
            )
            for index in range(1, 7)
        ]

        first_day = _buildRecommendedTracks(
            {},
            3,
            excludedTrackIds=existing_ids,
            recommendationDay="2026-05-02",
        )
        first_day_repeat = _buildRecommendedTracks(
            {},
            3,
            excludedTrackIds=existing_ids,
            recommendationDay="2026-05-02",
        )
        second_day = _buildRecommendedTracks(
            {},
            3,
            excludedTrackIds=existing_ids,
            recommendationDay="2026-05-03",
        )

        self.assertEqual(
            [track.id for track in first_day],
            [track.id for track in first_day_repeat],
        )
        self.assertNotEqual(
            [track.id for track in first_day],
            [track.id for track in second_day],
        )
        self.assertEqual(
            [track.id for track in first_day],
            [tied_tracks[4].id, tied_tracks[1].id, tied_tracks[5].id],
        )
        self.assertEqual(
            [track.id for track in second_day],
            [tied_tracks[2].id, tied_tracks[3].id, tied_tracks[5].id],
        )

    def test_create_recommend_playlist_is_idempotent_for_same_user_and_day(self):
        self._record_play(self.listened_tracks[0], 2)
        self._record_play(self.listened_tracks[1], 1)

        first_created = create_recommend_playlist(num_songs=3, user=self.user, day="2026-05-02")
        second_created = create_recommend_playlist(num_songs=3, user=self.user, day="2026-05-02")

        self.assertEqual(first_created, 1)
        self.assertEqual(second_created, 0)
        self.assertEqual(Playlist.select().where(Playlist.user == self.user).count(), 1)

    def test_create_recommend_playlist_archives_recommendations_older_than_retention_window(self):
        self._record_play(self.listened_tracks[0], 2)
        self._record_play(self.listened_tracks[1], 1)
        old_playlist = self._create_recommended_playlist("2026-05-01", self.candidate_tracks[0])
        retained_playlist = self._create_recommended_playlist("2026-05-03", self.candidate_tracks[1])

        created = create_recommend_playlist(
            num_songs=3,
            user=self.user,
            day="2026-05-07",
            config=self.config,
        )

        self.assertEqual(created, 1)
        self.assertIsNone(Playlist.get_or_none(Playlist.id == old_playlist.id))
        self.assertIsNotNone(Playlist.get_or_none(Playlist.id == retained_playlist.id))
        archive_path = os.path.join(
            self.config.WEBAPP["cache_dir"],
            "recommend-playlists",
            self.user.name,
            "2026-05-01.json",
        )
        self.assertTrue(os.path.isfile(archive_path))
        with open(archive_path, "r", encoding="utf-8") as archive_file:
            payload = json.load(archive_file)
        self.assertEqual(payload["playlist_id"], str(old_playlist.id))
        self.assertEqual(payload["user"], self.user.name)
        self.assertEqual(payload["recommendation_day"], "2026-05-01")
        self.assertEqual(payload["track_ids"], [str(self.candidate_tracks[0].id)])
        self.assertEqual(payload["tracks"][0]["title"], self.candidate_tracks[0].title)
        self.assertIn("rock", payload["tracks"][0]["recommend_reason"])

    def test_recommendation_reason_uses_track_metadata_when_available(self):
        track = self.candidate_tracks[0]
        TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            mood_json='["calm", "warm"]',
            scene_json='["late night"]',
            provider="test",
        )

        reasons = buildRecommendationReasonMap(self.user, [track])

        self.assertIn("calm / warm", reasons[str(track.id)])
        self.assertIn("late night", reasons[str(track.id)])

    def test_create_recommend_playlist_sanitizes_archive_user_directory(self):
        self._record_play(self.listened_tracks[0], 2)
        self._record_play(self.listened_tracks[1], 1)
        self._create_recommended_playlist("2026-05-01", self.candidate_tracks[0])
        self.user.name = "../alice"
        self.user.save()

        created = create_recommend_playlist(
            num_songs=3,
            user=self.user,
            day="2026-05-07",
            config=self.config,
        )

        archive_root = os.path.join(
            self.config.WEBAPP["cache_dir"],
            "recommend-playlists",
        )
        archive_path = os.path.join(archive_root, "alice", "2026-05-01.json")
        self.assertEqual(created, 1)
        self.assertTrue(os.path.isfile(archive_path))
        self.assertEqual(os.path.commonpath([archive_root, archive_path]), archive_root)
        self.assertFalse(os.path.exists(os.path.join(self.config.WEBAPP["cache_dir"], "alice")))

    def test_create_recommend_playlist_archives_old_entries_even_when_today_already_exists(self):
        self._create_recommended_playlist("2026-05-01", self.candidate_tracks[0])
        existing_today = self._create_recommended_playlist("2026-05-07", self.candidate_tracks[1])

        created = create_recommend_playlist(
            num_songs=3,
            user=self.user,
            day="2026-05-07",
            config=self.config,
        )

        self.assertEqual(created, 0)
        self.assertEqual(
            Playlist.select()
            .where(Playlist.user == self.user, Playlist.comment == RECOMMENDED_PLAYLIST_COMMENT)
            .count(),
            1,
        )
        self.assertIsNotNone(Playlist.get_or_none(Playlist.id == existing_today.id))
        archive_path = os.path.join(
            self.config.WEBAPP["cache_dir"],
            "recommend-playlists",
            self.user.name,
            "2026-05-01.json",
        )
        self.assertTrue(os.path.isfile(archive_path))

    def test_create_recommend_playlist_keeps_database_row_when_archive_write_fails(self):
        self._record_play(self.listened_tracks[0], 2)
        self._record_play(self.listened_tracks[1], 1)
        old_playlist = self._create_recommended_playlist("2026-05-01", self.candidate_tracks[0])

        with patch("supysonic.recommend.write_dict_to_json", side_effect=OSError("disk full")):
            created = create_recommend_playlist(
                num_songs=3,
                user=self.user,
                day="2026-05-07",
                config=self.config,
            )

        self.assertEqual(created, 1)
        self.assertIsNotNone(Playlist.get_or_none(Playlist.id == old_playlist.id))
        self.assertIsNotNone(
            Playlist.get_or_none(Playlist.name == "alice's 2026-05-07 recommend playlist")
        )


if __name__ == "__main__":
    unittest.main()
