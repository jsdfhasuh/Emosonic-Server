import os
import unittest

from supysonic.db import Album, Artist, Folder, Track, TrackMetadata, User
from supysonic.recommendation_feedback import set_recommendation_feedback
from supysonic.similar_tracks import get_similar_tracks

from ..testbase import TestBase


class SimilarTracksTestCase(TestBase):
    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.root = Folder.create(root=True, name="Root", path="/music")
        self.artist = Artist.create(name="Seed Artist")
        self.album = Album.create(name="Seed Album", artist=self.artist)

    def _create_track(
        self,
        title,
        number,
        genre="ambient",
        artist=None,
        album=None,
        play_count=0,
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

    def _create_artist_track(self, title, number, artist_name, genre="ambient"):
        artist = Artist.create(name=artist_name)
        album = Album.create(name=f"{artist_name} Album", artist=artist)
        return self._create_track(
            title,
            number,
            genre=genre,
            artist=artist,
            album=album,
        )

    def _metadata(
        self,
        track,
        *,
        mood=None,
        scene=None,
        tags=None,
        language="en",
        energy=35,
        valence=55,
        danceability=45,
        confidence=0.9,
        provider="llm",
        source="llm",
    ):
        return TrackMetadata.create(
            track=track,
            track_last_modification=track.last_modification,
            mood_json=self._json_list(mood),
            scene_json=self._json_list(scene),
            tags_json=self._json_list(tags),
            language=language,
            energy=energy,
            valence=valence,
            danceability=danceability,
            confidence=confidence,
            provider=provider,
            source=source,
        )

    def _json_list(self, values):
        if not values:
            return None
        return "[" + ",".join(f'"{value}"' for value in values) + "]"

    def test_mood_and_scene_matches_rank_first(self):
        seed = self._create_track("Seed", 1, genre="ambient")
        mood_scene = self._create_artist_track("Mood Scene", 2, "Other Artist")
        tag_only = self._create_artist_track("Tag Only", 3, "Third Artist")
        self._metadata(
            seed,
            mood=["calm"],
            scene=["late night"],
            tags=["dreamy"],
        )
        self._metadata(
            mood_scene,
            mood=["calm"],
            scene=["late night"],
            tags=["minimal"],
        )
        self._metadata(tag_only, mood=["bright"], scene=["workout"], tags=["dreamy"])

        results = get_similar_tracks(seed.id, limit=2)

        self.assertEqual(
            [result["track"].id for result in results],
            [mood_scene.id, tag_only.id],
        )
        self.assertTrue(any("mood: calm" in reason for reason in results[0]["reasons"]))
        self.assertTrue(
            any("scene: late night" in reason for reason in results[0]["reasons"])
        )

    def test_energy_close_candidate_ranks_ahead_of_far_candidate(self):
        seed = self._create_track("Seed", 1)
        near = self._create_artist_track("Near Energy", 2, "Near Artist")
        far = self._create_artist_track("Far Energy", 3, "Far Artist")
        self._metadata(seed, mood=["calm"], scene=["focus"], energy=30)
        self._metadata(near, mood=["calm"], scene=["focus"], energy=34)
        self._metadata(far, mood=["calm"], scene=["focus"], energy=95)

        results = get_similar_tracks(seed.id, limit=2)

        self.assertEqual([result["track"].id for result in results], [near.id, far.id])
        self.assertTrue(
            any("energy close: 34 vs 30" in reason for reason in results[0]["reasons"])
        )

    def test_local_metadata_does_not_force_candidate_to_top(self):
        seed = self._create_track("Seed", 1, genre="ambient")
        llm_match = self._create_artist_track(
            "LLM Match",
            2,
            "LLM Artist",
            genre="jazz",
        )
        local_match = self._create_artist_track(
            "Local Match",
            3,
            "Local Artist",
            genre="ambient",
        )
        self._metadata(seed, mood=["calm"], scene=["late night"], tags=["dreamy"])
        self._metadata(llm_match, mood=["calm"], scene=["late night"], tags=["minimal"])
        self._metadata(
            local_match,
            mood=["calm"],
            scene=["late night"],
            tags=["dreamy"],
            confidence=0.25,
            provider="local",
            source="local",
        )

        results = get_similar_tracks(seed.id, limit=2)

        self.assertEqual(results[0]["track"].id, llm_match.id)
        self.assertNotIn("mood: calm", results[1]["reasons"])
        self.assertEqual(results[1]["reasons"], ["genre: ambient", "artist variety"])

    def test_no_metadata_falls_back_to_genre(self):
        seed = self._create_track("Seed", 1, genre="rock")
        same_genre = self._create_artist_track(
            "Same Genre",
            2,
            "Rock Artist",
            genre="rock",
        )
        other_genre = self._create_artist_track(
            "Other Genre",
            3,
            "Jazz Artist",
            genre="jazz",
        )

        results = get_similar_tracks(seed.id, limit=10)

        self.assertEqual([result["track"].id for result in results], [same_genre.id])
        self.assertTrue(
            any("genre: rock" in reason for reason in results[0]["reasons"])
        )
        self.assertNotIn(other_genre.id, [result["track"].id for result in results])

    def test_preloaded_target_must_match_requested_track_id(self):
        seed = self._create_track("Seed", 1, genre="rock")
        other_seed = self._create_artist_track(
            "Other Seed",
            2,
            "Other Seed Artist",
            genre="jazz",
        )
        candidate = self._create_artist_track(
            "Same Genre",
            3,
            "Rock Artist",
            genre="rock",
        )

        results = get_similar_tracks(
            seed.id,
            limit=10,
            target=other_seed,
            candidates=[candidate],
        )

        self.assertEqual(results, [])

    def test_feedback_filters_hidden_and_disliked_tracks(self):
        seed = self._create_track("Seed", 1, genre="rock")
        disliked = self._create_artist_track(
            "Disliked",
            2,
            "Disliked Artist",
            genre="rock",
        )
        hidden_artist_track = self._create_artist_track(
            "Hidden",
            3,
            "Hidden Artist",
            genre="rock",
        )
        visible = self._create_artist_track(
            "Visible",
            4,
            "Visible Artist",
            genre="rock",
        )

        set_recommendation_feedback(self.user, str(disliked.id), "dislike")
        set_recommendation_feedback(
            self.user,
            str(hidden_artist_track.artist_id),
            "hide_artist",
        )

        results = get_similar_tracks(seed.id, limit=10, user=self.user)
        result_ids = [result["track"].id for result in results]

        self.assertEqual(result_ids, [visible.id])

    def test_artist_diversity_prevents_all_results_from_same_artist(self):
        seed = self._create_track("Seed", 1)
        same_artist_tracks = [
            self._create_track(f"Same Artist {index}", index + 2)
            for index in range(3)
        ]
        different_artist = self._create_artist_track(
            "Different Artist",
            10,
            "Other Artist",
        )
        self._metadata(seed, mood=["calm"], scene=["late night"], tags=["dreamy"])
        for track in same_artist_tracks:
            self._metadata(track, mood=["calm"], scene=["late night"], tags=["dreamy"])
        self._metadata(
            different_artist,
            mood=["bright"],
            scene=["morning"],
            tags=["dreamy"],
        )

        results = get_similar_tracks(seed.id, limit=3)

        self.assertEqual(len(results), 3)
        self.assertIn(different_artist.id, [result["track"].id for result in results])
        self.assertNotEqual(
            {result["track"].artist_id for result in results},
            {self.artist.id},
        )

    def test_returns_requested_limit_when_enough_genre_matches_exist(self):
        seed = self._create_track("Seed", 1, genre="rock")
        tracks = [
            self._create_artist_track(
                f"Candidate {index:02}",
                index + 2,
                f"Artist {index}",
                genre="rock",
            )
            for index in range(12)
        ]

        results = get_similar_tracks(seed.id, limit=10)

        self.assertEqual(len(results), 10)
        self.assertEqual(
            [result["track"].id for result in results],
            [track.id for track in tracks[:10]],
        )


if __name__ == "__main__":
    unittest.main()
