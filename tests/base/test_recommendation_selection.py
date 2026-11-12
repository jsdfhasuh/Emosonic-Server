import unittest
import uuid
from collections import Counter
from dataclasses import dataclass
from itertools import product
from math import ceil

from supysonic.recommendation_selection import select_diverse_recommendations


@dataclass(frozen=True)
class Candidate:
    id: object
    artist_id: object


class RecommendationSelectionTestCase(unittest.TestCase):
    def _tracks(self, artist, count):
        return [Candidate(f"{artist}-{index}", artist) for index in range(count)]

    def test_sufficient_pool_caps_artist_at_five_and_reserves_forty_fresh(self):
        recent = [
            track
            for artist in range(10)
            for track in self._tracks(f"recent-{artist}", 8)
        ]
        fresh = [
            track
            for artist in range(10)
            for track in self._tracks(f"fresh-{artist}", 8)
        ]
        candidates = recent + fresh
        recent_ids = {track.id for track in recent}

        selected = select_diverse_recommendations(
            candidates, 50, recent_ids, minimum_fresh_tracks=40
        )

        self.assertEqual(len(selected), 50)
        self.assertEqual(sum(track.id not in recent_ids for track in selected), 40)
        self.assertEqual(max(Counter(t.artist_id for t in selected).values()), 5)
        self.assertEqual(selected, [track for track in candidates if track in selected])

    def test_infeasible_freshness_does_not_relax_artist_cap(self):
        fresh = self._tracks("fresh", 30)
        recent = [
            track
            for artist in range(10)
            for track in self._tracks(f"recent-{artist}", 8)
        ]
        recent_ids = {track.id for track in recent}

        selected = select_diverse_recommendations(
            fresh + recent, 50, recent_ids, minimum_fresh_tracks=40
        )

        self.assertEqual(len(selected), 50)
        self.assertEqual(sum(track.id not in recent_ids for track in selected), 5)
        self.assertEqual(max(Counter(t.artist_id for t in selected).values()), 5)

    def test_artist_cap_relaxes_only_to_minimum_capacity(self):
        candidates = self._tracks("large", 20) + self._tracks("small", 2)

        selected = select_diverse_recommendations(candidates, 10)

        self.assertEqual(len(selected), 10)
        self.assertEqual(
            Counter(t.artist_id for t in selected), {"large": 8, "small": 2}
        )

    def test_relaxation_can_still_satisfy_freshness_target(self):
        candidates = self._tracks("first", 10) + self._tracks("second", 10)
        recent_ids = {track.id for track in candidates[:4] + candidates[10:14]}

        selected = select_diverse_recommendations(
            candidates, 10, recent_ids, minimum_fresh_tracks=8
        )

        self.assertEqual(len(selected), 10)
        self.assertEqual(
            Counter(t.artist_id for t in selected), {"first": 5, "second": 5}
        )
        self.assertGreaterEqual(
            sum(track.id not in recent_ids for track in selected), 8
        )

    def test_single_artist_pool_fills_requested_size(self):
        candidates = self._tracks("only", 8)

        self.assertEqual(
            select_diverse_recommendations(candidates, 5), candidates[:5]
        )

    def test_short_catalog_returns_all_candidates(self):
        candidates = self._tracks("only", 3)

        self.assertEqual(select_diverse_recommendations(candidates, 50), candidates)

    def test_empty_and_nonpositive_count_return_empty(self):
        self.assertEqual(select_diverse_recommendations([], 50), [])
        candidates = self._tracks("only", 3)
        self.assertEqual(select_diverse_recommendations(candidates, 0), [])
        self.assertEqual(select_diverse_recommendations(candidates, -1), [])

    def test_duplicate_ids_keep_first_object_without_inflating_capacity(self):
        first = Candidate("same", "first")
        duplicate = Candidate("same", "different")
        candidates = [first, duplicate, Candidate("second", "first")]

        selected = select_diverse_recommendations(candidates, 3)

        self.assertEqual(selected, [first, candidates[2]])
        self.assertIs(selected[0], first)

    def test_uuid_and_string_ids_share_duplicate_and_recent_identity(self):
        track_id = uuid.uuid4()
        candidates = [
            Candidate(track_id, "one"), Candidate(str(track_id), "two")
        ]
        fresh = Candidate("fresh", "three")

        selected = select_diverse_recommendations(
            candidates + [fresh], 1, [str(track_id)], minimum_fresh_tracks=1
        )

        self.assertEqual(selected, [fresh])

    def test_unknown_artists_share_one_cap_bucket(self):
        unknown = [
            Candidate(f"unknown-{index}", None if index % 2 else "")
            for index in range(10)
        ]
        known = [
            Candidate(f"known-{index}", f"artist-{index}")
            for index in range(10)
        ]

        selected = select_diverse_recommendations(unknown + known, 10)

        self.assertEqual(len(selected), 10)
        self.assertEqual(sum(not track.artist_id for track in selected), 1)

    def test_selection_is_deterministic_and_does_not_mutate_inputs(self):
        candidates = self._tracks("first", 8) + self._tracks("second", 8)
        original = list(candidates)
        recent_ids = {track.id for track in candidates[:5]}
        original_recent_ids = set(recent_ids)

        first = select_diverse_recommendations(candidates, 6, recent_ids, 4)
        second = select_diverse_recommendations(candidates, 6, recent_ids, 4)

        self.assertEqual(first, second)
        self.assertEqual(candidates, original)
        self.assertEqual(recent_ids, original_recent_ids)

    def test_explicit_ratio_and_rounding(self):
        candidates = [
            track
            for artist in range(5)
            for track in self._tracks(str(artist), 10)
        ]

        selected = select_diverse_recommendations(
            candidates, 11, max_artist_ratio=0.2
        )

        self.assertEqual(len(selected), 11)
        self.assertEqual(max(Counter(t.artist_id for t in selected).values()), 3)
        self.assertEqual(
            select_diverse_recommendations(candidates, 10, max_artist_ratio=1),
            candidates[:10],
        )

    def test_invalid_ratios_fall_back_to_default(self):
        candidates = [
            track
            for artist in range(10)
            for track in self._tracks(str(artist), 10)
        ]
        expected = select_diverse_recommendations(candidates, 50)

        invalid_ratios = (
            float("nan"), float("inf"), -float("inf"), 0, -1, 1.1, None, "invalid"
        )
        for ratio in invalid_ratios:
            with self.subTest(ratio=ratio):
                self.assertEqual(
                    select_diverse_recommendations(
                        candidates, 50, max_artist_ratio=ratio
                    ),
                    expected,
                )

    def test_fresh_target_is_clamped_to_selection_size(self):
        candidates = self._tracks("one", 3) + self._tracks("two", 3)
        recent_ids = {track.id for track in candidates[:3]}

        selected = select_diverse_recommendations(candidates, 4, recent_ids, 100)

        self.assertEqual(len(selected), 4)
        self.assertEqual(sum(track.id not in recent_ids for track in selected), 2)
        self.assertEqual(
            select_diverse_recommendations(candidates, 4, recent_ids, -1),
            select_diverse_recommendations(candidates, 4, recent_ids, 0),
        )

    def test_artist_pool_matrix_fills_at_minimal_cap_with_feasible_freshness(self):
        for pool_sizes in product((1, 2, 5), repeat=3):
            candidates = [
                track
                for artist, count in enumerate(pool_sizes)
                for track in self._tracks(str(artist), count)
            ]
            recent_ids = {track.id for track in candidates[::2]}
            fresh_pool = Counter(
                track.artist_id
                for track in candidates
                if track.id not in recent_ids
            )
            for requested in range(1, 13):
                with self.subTest(pool_sizes=pool_sizes, requested=requested):
                    target = min(requested, len(candidates))
                    cap = max(1, ceil(requested * 0.1))
                    while sum(min(count, cap) for count in pool_sizes) < target:
                        cap += 1
                    fresh_target = min(target, ceil(requested * 0.8))
                    selected = select_diverse_recommendations(
                        candidates,
                        requested,
                        recent_ids,
                        minimum_fresh_tracks=fresh_target,
                    )
                    self.assertEqual(len(selected), target)
                    self.assertLessEqual(
                        max(Counter(t.artist_id for t in selected).values()), cap
                    )
                    self.assertGreaterEqual(
                        sum(t.id not in recent_ids for t in selected),
                        min(
                            fresh_target,
                            sum(min(count, cap) for count in fresh_pool.values()),
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
