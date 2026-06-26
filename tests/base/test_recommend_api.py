import os
import unittest

from unittest.mock import patch

from supysonic.db import (
    Album,
    Artist,
    Folder,
    Playlist,
    Track,
    User,
    User_Play_Activity,
    UserRecommendationFeedback,
)
from supysonic.recommend import RECOMMENDED_PLAYLIST_COMMENT

from ..testbase import TestBase


class RecommendApiTestCase(TestBase):
    __with_api__ = True

    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Artist")
        self.album = Album.create(name="Album", artist=self.artist)
        self.track = self._create_track("Song", 1, genre="rock")

    def _create_track(
        self,
        title,
        number,
        genre=None,
        play_count=0,
        artist=None,
        album=None,
    ):
        artist = artist or self.artist
        album = album or self.album
        return Track.create(
            disc=1,
            number=number,
            title=title,
            duration=180,
            has_art=False,
            album=album,
            artist=artist,
            genre=genre,
            bitrate=320,
            play_count=play_count,
            path=os.path.join("/music", f"{number}.flac"),
            last_modification=1,
            root_folder=self.root,
            folder=self.root,
        )

    def _get_recommended_playlist(self):
        return self.client.get(
            "/rest/getRecommendedPlaylists.view?u=alice&p=Alic3&c=tests&f=json"
        )

    def _post_feedback(
        self,
        action="dislike",
        user="alice",
        password="Alic3",
        target_id=None,
        target_type=None,
        scope="hot_recommended",
    ):
        query = {
            "u": user,
            "p": password,
            "c": "tests",
            "f": "json",
            "id": str(target_id or self.track.id),
            "action": action,
            "scope": scope,
            "reason": "user_dislike",
            "source": "emosonic",
        }
        if target_type:
            query["targetType"] = target_type
        return self.client.post(
            "/rest/setRecommendationFeedback.view",
            query_string=query,
            json={},
        )

    def _post_feedback_payload(self, query=None, json_payload=None):
        payload = {
            "u": "alice",
            "p": "Alic3",
            "c": "tests",
            "f": "json",
        }
        payload.update(query or {})
        return self.client.post(
            "/rest/setRecommendationFeedback.view",
            query_string=payload,
            json=json_payload or {},
        )

    def _get_feedback(self, user="alice", password="Alic3", scope="hot_recommended"):
        return self.client.get(
            "/rest/getRecommendationFeedback.view",
            query_string={
                "u": user,
                "p": password,
                "c": "tests",
                "f": "json",
                "scope": scope,
            },
        )

    def _create_recommended_playlist(self, user, *tracks):
        playlist = Playlist.create(
            user=user,
            name=f"{user.name}'s 2026-05-24 recommend playlist",
            comment=RECOMMENDED_PLAYLIST_COMMENT,
        )
        for track in tracks:
            playlist.add(track)
        playlist.save()
        return playlist

    def test_recommended_playlist_api_does_not_submit_background_generation_task(self):
        with patch("supysonic.TaskManger.TaskManager.submit_task") as submit_task:
            rv = self._get_recommended_playlist()

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.json["subsonic-response"]["status"], "ok")
        submit_task.assert_not_called()

    def test_recommended_playlist_api_falls_back_to_latest_recommended_playlist(self):
        playlist = Playlist.create(
            user=self.user,
            name="alice's 2026-05-01 recommend playlist",
            comment=RECOMMENDED_PLAYLIST_COMMENT,
        )
        playlist.add(self.track)
        playlist.save()

        rv = self._get_recommended_playlist()

        self.assertEqual(rv.status_code, 200)
        payload = rv.json["subsonic-response"]["playlist"]
        self.assertEqual(payload["name"], "alice's 2026-05-01 recommend playlist")
        self.assertEqual(payload["songCount"], 1)

    def test_recommended_playlist_entries_include_recommend_reason(self):
        listened = self._create_track("Listened Rock", 2, genre="rock")
        recommended = self._create_track("Recommended Rock", 3, genre="rock")
        User_Play_Activity.create(track=listened, user=self.user)
        self._create_recommended_playlist(self.user, recommended)

        rv = self._get_recommended_playlist()

        payload = rv.json["subsonic-response"]["playlist"]
        reason = payload["entry"][0]["recommendReason"]
        self.assertIn("rock", reason)
        self.assertIn("often listen", reason)

    def test_set_recommendation_feedback_dislike_is_idempotent(self):
        first = self._post_feedback("dislike")
        second = self._post_feedback("dislike")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json["subsonic-response"]["status"], "ok")
        payload = first.json["subsonic-response"]["recommendationFeedback"]
        self.assertEqual(payload["id"], str(self.track.id))
        self.assertEqual(payload["action"], "dislike")
        self.assertEqual(payload["scope"], "hot_recommended")
        self.assertEqual(UserRecommendationFeedback.select().count(), 1)

    def test_set_recommendation_feedback_logs_safe_update(self):
        with self.assertLogs("supysonic.api.playlists", level="INFO") as logs:
            rv = self._post_feedback(
                "hide_artist",
                target_id=self.artist.id,
                target_type="artist",
            )

        self.assertEqual(rv.status_code, 200)
        logged = "\n".join(logs.output)
        self.assertIn("recommendation event=feedback_updated", logged)
        self.assertIn("user=alice", logged)
        self.assertIn("target_type=artist", logged)
        self.assertIn("action=hide_artist", logged)
        self.assertIn("source=emosonic", logged)
        self.assertIn("restored=false", logged)
        self.assertNotIn(str(self.artist.id), logged)

    def test_set_recommendation_feedback_restore_removes_active_dislike(self):
        self._post_feedback("dislike")

        rv = self._post_feedback("restore")

        self.assertEqual(rv.status_code, 200)
        payload = rv.json["subsonic-response"]["recommendationFeedback"]
        self.assertEqual(payload["action"], "restore")
        feedback = UserRecommendationFeedback.get()
        self.assertIsNotNone(feedback.deleted_at)

    def test_set_recommendation_feedback_accepts_artist_and_like_more_actions(self):
        hidden_artist = Artist.create(name="Hidden Artist")

        hidden = self._post_feedback(
            "hide_artist",
            target_id=hidden_artist.id,
        )
        liked = self._post_feedback("like_more")

        self.assertEqual(hidden.status_code, 200)
        hidden_payload = hidden.json["subsonic-response"]["recommendationFeedback"]
        self.assertEqual(hidden_payload["targetType"], "artist")
        self.assertEqual(hidden_payload["targetId"], str(hidden_artist.id))
        self.assertEqual(hidden_payload["action"], "hide_artist")
        self.assertEqual(liked.status_code, 200)
        self.assertEqual(UserRecommendationFeedback.select().count(), 2)

        rv = self._get_feedback()
        payload = rv.json["subsonic-response"]["recommendationFeedback"]
        self.assertEqual(payload["hiddenArtistIds"], [str(hidden_artist.id)])
        self.assertEqual(payload["likedMoreSongIds"], [str(self.track.id)])

    def test_set_recommendation_feedback_accepts_target_id_aliases(self):
        query_alias = self._post_feedback_payload(
            query={
                "targetId": str(self.artist.id),
                "targetType": "artist",
                "action": "hide_artist",
            }
        )
        body_alias = self._post_feedback_payload(
            query={"action": "hide_album"},
            json_payload={
                "target_id": str(self.album.id),
                "targetType": "album",
            },
        )

        self.assertEqual(query_alias.status_code, 200)
        self.assertEqual(body_alias.status_code, 200)
        query_payload = query_alias.json["subsonic-response"][
            "recommendationFeedback"
        ]
        body_payload = body_alias.json["subsonic-response"][
            "recommendationFeedback"
        ]
        self.assertEqual(query_payload["targetId"], str(self.artist.id))
        self.assertEqual(query_payload["target_id"], str(self.artist.id))
        self.assertEqual(query_payload["targetType"], "artist")
        self.assertEqual(body_payload["targetId"], str(self.album.id))
        self.assertEqual(body_payload["target_id"], str(self.album.id))
        self.assertEqual(body_payload["targetType"], "album")

    def test_get_recommendation_feedback_returns_and_restores_extended_targets(self):
        hidden_album = self._post_feedback(
            "hide_album",
            target_id=self.album.id,
        )
        hidden_style = self._post_feedback(
            "not_this_style",
            target_id="Rock",
        )
        liked_more = self._post_feedback("like_more")

        self.assertEqual(hidden_album.status_code, 200)
        self.assertEqual(hidden_style.status_code, 200)
        self.assertEqual(liked_more.status_code, 200)

        rv = self._get_feedback()
        payload = rv.json["subsonic-response"]["recommendationFeedback"]
        self.assertEqual(payload["hiddenAlbumIds"], [str(self.album.id)])
        self.assertEqual(payload["hiddenGenres"], ["rock"])
        self.assertEqual(payload["likedMoreSongIds"], [str(self.track.id)])

        self._post_feedback("restore_album", target_id=self.album.id)
        self._post_feedback("restore_style", target_id="rock")
        self._post_feedback("restore_song")

        restored = self._get_feedback()
        restored_payload = restored.json["subsonic-response"]["recommendationFeedback"]
        self.assertNotIn("hiddenAlbumIds", restored_payload)
        self.assertNotIn("hiddenGenres", restored_payload)
        self.assertNotIn("likedMoreSongIds", restored_payload)
        self.assertTrue(restored_payload["updatedAt"])

    def test_get_recommendation_feedback_excludes_agent_artist_names_from_artist_ids(self):
        self._post_feedback("hide_artist", target_id=self.artist.id)
        self._post_feedback("hide_artist", target_id="External Agent Artist")

        rv = self._get_feedback()

        payload = rv.json["subsonic-response"]["recommendationFeedback"]
        self.assertEqual(payload["hiddenArtistIds"], [str(self.artist.id)])
        self.assertEqual(payload["hiddenArtistNames"], ["External Agent Artist"])
        self.assertNotIn("External Agent Artist", payload["hiddenArtistIds"])
        self.assertNotIn(str(self.artist.id), payload["hiddenArtistNames"])

    def test_set_recommendation_feedback_rejects_invalid_action(self):
        rv = self._post_feedback("hide_forever")

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.json["subsonic-response"]["status"], "failed")
        error = rv.json["subsonic-response"]["error"]
        self.assertEqual(error["code"], 0)
        self.assertEqual(error["message"], "invalid recommendation feedback action")
        self.assertEqual(UserRecommendationFeedback.select().count(), 0)

    def test_recommendation_feedback_api_rejects_invalid_scope(self):
        set_response = self._post_feedback(
            "dislike",
            scope="daily_mix",
        )
        get_response = self._get_feedback(scope="daily_mix")

        self.assertEqual(set_response.status_code, 200)
        self.assertEqual(
            set_response.json["subsonic-response"]["status"],
            "failed",
        )
        self.assertEqual(
            set_response.json["subsonic-response"]["error"]["message"],
            "invalid recommendation feedback scope",
        )
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(
            get_response.json["subsonic-response"]["status"],
            "failed",
        )
        self.assertEqual(
            get_response.json["subsonic-response"]["error"]["message"],
            "invalid recommendation feedback scope",
        )
        self.assertEqual(UserRecommendationFeedback.select().count(), 0)

    def test_set_recommendation_feedback_rejects_mismatched_target_type(self):
        rv = self._post_feedback(
            "hide_artist",
            target_id=self.artist.id,
            target_type="song",
        )

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.json["subsonic-response"]["status"], "failed")
        error = rv.json["subsonic-response"]["error"]
        self.assertEqual(error["code"], 0)
        self.assertEqual(
            error["message"],
            "recommendation feedback target type does not match action",
        )
        self.assertEqual(UserRecommendationFeedback.select().count(), 0)

    def test_set_recommendation_feedback_generic_restore_accepts_target_type(self):
        self._post_feedback(
            "hide_artist",
            target_id=self.artist.id,
        )
        rv = self._post_feedback(
            "restore",
            target_id=self.artist.id,
            target_type="artist",
        )

        self.assertEqual(rv.status_code, 200)
        self.assertEqual(rv.json["subsonic-response"]["status"], "ok")
        payload = rv.json["subsonic-response"]["recommendationFeedback"]
        self.assertEqual(payload["targetType"], "artist")
        self.assertEqual(payload["targetId"], str(self.artist.id))
        self.assertEqual(payload["action"], "restore")
        feedback = UserRecommendationFeedback.get()
        self.assertEqual(feedback.target_type, "artist")
        self.assertEqual(feedback.action, "restore")
        self.assertIsNotNone(feedback.deleted_at)

    def test_get_recommended_playlist_filters_disliked_tracks_for_current_user(self):
        playlist = Playlist.create(
            user=self.user,
            name="alice's 2026-05-01 recommend playlist",
            comment=RECOMMENDED_PLAYLIST_COMMENT,
        )
        playlist.add(self.track)
        playlist.save()
        self._post_feedback("dislike")

        rv = self._get_recommended_playlist()

        payload = rv.json["subsonic-response"]["playlist"]
        self.assertEqual(payload["songCount"], 0)
        self.assertNotIn("entry", payload)

    def test_get_recommended_playlist_backfills_after_filtering_disliked_tracks(self):
        keeper = self._create_track("Keeper", 2, genre="rock", play_count=4)
        same_genre = self._create_track("Same Genre", 3, genre="rock", play_count=8)
        same_artist = self._create_track("Same Artist", 4, genre="jazz", play_count=6)
        self._create_recommended_playlist(self.user, self.track, keeper)
        self._post_feedback("dislike")

        with self.assertLogs("supysonic.api.playlists", level="INFO") as logs:
            rv = self.client.get(
                "/rest/getRecommendedPlaylists.view",
                query_string={
                    "u": "alice",
                    "p": "Alic3",
                    "c": "tests",
                    "f": "json",
                    "count": "3",
                },
            )

        payload = rv.json["subsonic-response"]["playlist"]
        entry_ids = [entry["id"] for entry in payload["entry"]]
        self.assertEqual(payload["songCount"], 3)
        self.assertEqual(len(entry_ids), len(set(entry_ids)))
        self.assertNotIn(str(self.track.id), entry_ids)
        self.assertIn(str(keeper.id), entry_ids)
        self.assertIn(str(same_genre.id), entry_ids)
        self.assertIn(str(same_artist.id), entry_ids)
        logged = "\n".join(logs.output)
        self.assertIn("recommendation event=playlist_served", logged)
        self.assertIn("user=alice", logged)
        self.assertIn("source=playlist", logged)
        self.assertIn("requested_count=3", logged)
        self.assertIn("source_track_count=2", logged)
        self.assertIn("returned_count=3", logged)
        self.assertIn("filtered_feedback_track_count=1", logged)
        self.assertIn("backfilled_track_count=2", logged)
        self.assertIn("disliked_song_count=1", logged)
        self.assertNotIn(str(self.track.id), logged)
        self.assertNotIn("Song", logged)

    def test_get_recommended_playlist_backfills_from_like_more_seed(self):
        keeper = self._create_track("Keeper", 2, genre="sparse", play_count=4)
        seed_artist = Artist.create(name="Seed Artist")
        seed_album = Album.create(name="Seed Album", artist=seed_artist)
        seed_track = self._create_track(
            "Liked Seed",
            3,
            genre="ambient",
            play_count=0,
            artist=seed_artist,
            album=seed_album,
        )
        similar_track = self._create_track(
            "Similar To Liked Seed",
            4,
            genre="ambient",
            play_count=10,
            artist=seed_artist,
            album=seed_album,
        )
        popular_fallback = self._create_track(
            "Popular But Unrelated",
            5,
            genre="pop",
            play_count=100,
        )
        self._create_recommended_playlist(self.user, self.track, keeper)
        self._post_feedback("dislike")
        self._post_feedback("like_more", target_id=seed_track.id)

        rv = self.client.get(
            "/rest/getRecommendedPlaylists.view",
            query_string={
                "u": "alice",
                "p": "Alic3",
                "c": "tests",
                "f": "json",
                "count": "2",
            },
        )

        payload = rv.json["subsonic-response"]["playlist"]
        entry_ids = [entry["id"] for entry in payload["entry"]]
        self.assertEqual(payload["songCount"], 2)
        self.assertIn(str(keeper.id), entry_ids)
        self.assertIn(str(similar_track.id), entry_ids)
        self.assertNotIn(str(self.track.id), entry_ids)
        self.assertNotIn(str(popular_fallback.id), entry_ids)

    def test_get_recommended_playlist_filters_hidden_artist_feedback(self):
        hidden_artist = Artist.create(name="Hidden Artist")
        hidden_album = Album.create(name="Hidden Album", artist=hidden_artist)
        visible_artist = Artist.create(name="Visible Artist")
        visible_album = Album.create(name="Visible Album", artist=visible_artist)
        hidden = self._create_track(
            "Hidden Artist Song",
            5,
            genre="rock",
            play_count=20,
            artist=hidden_artist,
            album=hidden_album,
        )
        visible = self._create_track(
            "Visible Artist Song",
            6,
            genre="rock",
            play_count=10,
            artist=visible_artist,
            album=visible_album,
        )
        visible_backfill = self._create_track(
            "Visible Backfill",
            7,
            genre="rock",
            play_count=8,
            artist=visible_artist,
            album=visible_album,
        )
        self._create_recommended_playlist(self.user, hidden, visible)
        self._post_feedback("hide_artist", target_id=hidden_artist.id)

        rv = self.client.get(
            "/rest/getRecommendedPlaylists.view",
            query_string={
                "u": "alice",
                "p": "Alic3",
                "c": "tests",
                "f": "json",
                "count": "2",
            },
        )

        payload = rv.json["subsonic-response"]["playlist"]
        entry_ids = [entry["id"] for entry in payload["entry"]]
        artist_ids = {entry["artistId"] for entry in payload["entry"]}
        self.assertEqual(payload["songCount"], 2)
        self.assertNotIn(str(hidden.id), entry_ids)
        self.assertNotIn(str(hidden_artist.id), artist_ids)
        self.assertIn(str(visible.id), entry_ids)
        self.assertIn(str(visible_backfill.id), entry_ids)

        self._post_feedback("restore_artist", target_id=hidden_artist.id)
        restored = self._get_recommended_playlist()
        restored_ids = [
            entry["id"]
            for entry in restored.json["subsonic-response"]["playlist"]["entry"]
        ]
        self.assertIn(str(hidden.id), restored_ids)

    def test_get_recommended_playlist_filters_hidden_artist_name_feedback(self):
        hidden_artist = Artist.create(name="Hidden Artist")
        hidden_album = Album.create(name="Hidden Album", artist=hidden_artist)
        visible_artist = Artist.create(name="Visible Artist")
        visible_album = Album.create(name="Visible Album", artist=visible_artist)
        hidden = self._create_track(
            "Hidden Artist Song",
            8,
            genre="rock",
            play_count=20,
            artist=hidden_artist,
            album=hidden_album,
        )
        visible = self._create_track(
            "Visible Artist Song",
            9,
            genre="rock",
            play_count=10,
            artist=visible_artist,
            album=visible_album,
        )
        self._create_recommended_playlist(self.user, hidden, visible)
        self._post_feedback("hide_artist", target_id="hidden   artist")

        rv = self._get_recommended_playlist()

        payload = rv.json["subsonic-response"]["playlist"]
        entry_ids = [entry["id"] for entry in payload["entry"]]
        artist_ids = {entry["artistId"] for entry in payload["entry"]}
        self.assertNotIn(str(hidden.id), entry_ids)
        self.assertNotIn(str(hidden_artist.id), artist_ids)
        self.assertIn(str(visible.id), entry_ids)

        self._post_feedback("restore_artist", target_id="hidden   artist")
        restored = self._get_recommended_playlist()
        restored_ids = [
            entry["id"]
            for entry in restored.json["subsonic-response"]["playlist"]["entry"]
        ]
        self.assertIn(str(hidden.id), restored_ids)

    def test_get_recommended_playlist_filters_hidden_album_and_style_feedback(self):
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
        visible_artist = Artist.create(name="Visible Artist")
        visible_album = Album.create(name="Visible Album", artist=visible_artist)
        hidden_album_track = self._create_track(
            "Hidden Album Song",
            8,
            genre="open",
            play_count=20,
            artist=hidden_album_artist,
            album=hidden_album,
        )
        hidden_style_track = self._create_track(
            "Hidden Style Song",
            9,
            genre="Blocked",
            play_count=18,
            artist=hidden_style_artist,
            album=hidden_style_album,
        )
        visible = self._create_track(
            "Visible Song",
            10,
            genre="open",
            play_count=12,
            artist=visible_artist,
            album=visible_album,
        )
        visible_backfill = self._create_track(
            "Visible Backfill",
            11,
            genre="open",
            play_count=9,
            artist=visible_artist,
            album=visible_album,
        )
        self._create_recommended_playlist(
            self.user,
            hidden_album_track,
            hidden_style_track,
            visible,
        )
        self._post_feedback("hide_album", target_id=hidden_album.id)
        self._post_feedback("not_this_style", target_id="blocked")

        rv = self.client.get(
            "/rest/getRecommendedPlaylists.view",
            query_string={
                "u": "alice",
                "p": "Alic3",
                "c": "tests",
                "f": "json",
                "count": "2",
            },
        )

        payload = rv.json["subsonic-response"]["playlist"]
        entry_ids = [entry["id"] for entry in payload["entry"]]
        album_ids = {entry["albumId"] for entry in payload["entry"]}
        genres = {entry.get("genre", "").casefold() for entry in payload["entry"]}
        self.assertEqual(payload["songCount"], 2)
        self.assertNotIn(str(hidden_album_track.id), entry_ids)
        self.assertNotIn(str(hidden_style_track.id), entry_ids)
        self.assertNotIn(str(hidden_album.id), album_ids)
        self.assertNotIn("blocked", genres)
        self.assertIn(str(visible.id), entry_ids)
        self.assertIn(str(visible_backfill.id), entry_ids)

        self._post_feedback("restore_album", target_id=hidden_album.id)
        self._post_feedback("restore_style", target_id="BLOCKED")
        restored = self._get_recommended_playlist()
        restored_ids = [
            entry["id"]
            for entry in restored.json["subsonic-response"]["playlist"]["entry"]
        ]
        self.assertIn(str(hidden_album_track.id), restored_ids)
        self.assertIn(str(hidden_style_track.id), restored_ids)

    def test_recommendation_feedback_is_isolated_per_user(self):
        bob = User.get(User.name == "bob")
        self._create_recommended_playlist(self.user, self.track)
        self._create_recommended_playlist(bob, self.track)
        self._post_feedback("dislike", user="alice", password="Alic3")

        rv = self.client.get(
            "/rest/getRecommendedPlaylists.view",
            query_string={"u": "bob", "p": "B0b", "c": "tests", "f": "json"},
        )

        payload = rv.json["subsonic-response"]["playlist"]
        self.assertEqual(payload["songCount"], 1)
        self.assertEqual(payload["entry"][0]["id"], str(self.track.id))

    def test_get_recommendation_feedback_returns_active_dislikes(self):
        self._post_feedback("dislike")

        rv = self._get_feedback()

        payload = rv.json["subsonic-response"]["recommendationFeedback"]
        self.assertEqual(payload["scope"], "hot_recommended")
        self.assertEqual(payload["dislikedSongIds"], [str(self.track.id)])
        self.assertTrue(payload["updatedAt"])

    def test_get_recommendation_feedback_is_isolated_per_user(self):
        self._post_feedback("dislike", user="alice", password="Alic3")

        rv = self._get_feedback(user="bob", password="B0b")

        payload = rv.json["subsonic-response"]["recommendationFeedback"]
        self.assertEqual(payload["scope"], "hot_recommended")
        self.assertNotIn("dislikedSongIds", payload)

    def test_get_recommendation_feedback_excludes_restored_tracks(self):
        self._post_feedback("dislike")
        self._post_feedback("restore")

        rv = self._get_feedback()

        payload = rv.json["subsonic-response"]["recommendationFeedback"]
        self.assertEqual(payload["scope"], "hot_recommended")
        self.assertNotIn("dislikedSongIds", payload)
        self.assertTrue(payload["updatedAt"])

    def test_restored_track_can_return_to_recommended_playlist(self):
        self._create_recommended_playlist(self.user, self.track)
        self._post_feedback("dislike")
        self._post_feedback("restore")

        rv = self._get_recommended_playlist()

        payload = rv.json["subsonic-response"]["playlist"]
        self.assertEqual(payload["songCount"], 1)
        self.assertEqual(payload["entry"][0]["id"], str(self.track.id))


if __name__ == "__main__":
    unittest.main()
