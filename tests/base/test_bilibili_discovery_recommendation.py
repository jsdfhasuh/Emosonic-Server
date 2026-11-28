import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from supysonic.album_discovery_quota import SHANGHAI
from supysonic.bilibili_discovery.recommendation import (
    BilibiliReadyRecording,
    build_bilibili_daily_pool,
    merge_bilibili_candidates,
)
from supysonic.db import (
    Album,
    AlbumDiscoveryPlacement,
    AlbumDiscoverySelection,
    Artist,
    BilibiliDiscoveryCandidate,
    BilibiliDiscoveryRun,
    Folder,
    Track,
    User,
    User_Play_Activity,
)
from supysonic.metadata_pricing import MODEL, PROFILE_MODEL
from supysonic.openai_recommendations import OpenAIRecommendationError
from supysonic.recommend import create_recommend_playlist, getRecommendedPlaylistForDay
from supysonic.recommendation_feedback import (
    get_recommendation_feedback_preferences,
    set_recommendation_feedback,
)
from supysonic.recommendation_openai_daily import (
    _daily_context,
    _encode_context,
    select_openai_daily_tracks,
)
from supysonic.user_listening_profile import build_recommendation_play_weights

from ..testbase import TestBase


def _track_ids(tracks):
    return tuple(str(track.id) for track in tracks)


class BilibiliDailyMergeTestCase(unittest.TestCase):
    reference = datetime(2026, 11, 28, 1, tzinfo=timezone.utc)

    def _recording(self, track, kind="direct", hour=1, source_days_ago=1):
        return BilibiliReadyRecording(
            track, self.reference.replace(tzinfo=None) - timedelta(hours=hour),
            self.reference.astimezone(SHANGHAI).date() - timedelta(days=source_days_ago),
            (kind,),
        )

    def test_full_pool_annotates_overlaps_displaces_only_unannotated_tail(self):
        baseline = [SimpleNamespace(id=str(index)) for index in range(150)]
        one, two = SimpleNamespace(id="new-a"), SimpleNamespace(id="new-b")
        recordings = [
            *[self._recording(baseline[index]) for index in (0, -1, -2)],
            self._recording(one, "style"), self._recording(one, "direct", hour=2),
            self._recording(two, "collaboration"),
        ]
        pool = merge_bilibili_candidates(baseline, recordings, 150, at=self.reference)
        self.assertEqual(len(pool.tracks), 150)
        self.assertEqual(pool.added_ids, ("new-a", "new-b"))
        self.assertEqual(pool.displaced_ids, ("146", "147"))
        self.assertEqual(set(pool.signals), {"0", "148", "149", "new-a", "new-b"})
        self.assertEqual(pool.signals["new-a"].kinds, ("style",))
        self.assertEqual(pool.tracks, (*baseline[:146], *baseline[148:], one, two))

    def test_small_and_zero_capacity_and_no_replaceable_positions(self):
        one, two, three = (SimpleNamespace(id=str(index)) for index in range(3))
        pool = merge_bilibili_candidates(
            [one, one], [self._recording(three), self._recording(two)], 3, at=self.reference,
        )
        self.assertEqual(pool.tracks, (one, two))
        self.assertEqual(pool.displaced_ids, ())
        self.assertEqual(pool.added_ids, ("1",))
        recordings = [self._recording(one), self._recording(two)]
        self.assertEqual(
            merge_bilibili_candidates([one], recordings, 1, at=self.reference).tracks,
            (one,),
        )
        self.assertEqual(
            merge_bilibili_candidates([one], recordings, 0, at=self.reference).tracks,
            (),
        )

    def test_caps_new_uuids_and_uses_day_then_uuid_when_ready_time_tied(self):
        recordings = [
            self._recording(SimpleNamespace(id=f"{index:02}"), source_days_ago=3)
            for index in range(15)
        ]
        recordings.extend((
            self._recording(SimpleNamespace(id="day-priority"), source_days_ago=2),
            self._recording(SimpleNamespace(id="time-priority"), hour=0, source_days_ago=5),
        ))
        pool = merge_bilibili_candidates([], reversed(recordings), 150, at=self.reference)
        self.assertEqual(pool.added_ids, (
            "time-priority", "day-priority", "00", "01", "02", "03", "04", "05", "06", "07",
        ))


class BilibiliDailyRecommendationTestCase(TestBase):
    __with_api__ = True

    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.reference = datetime.now(timezone.utc).replace(microsecond=0)
        self.day = self.reference.astimezone(SHANGHAI).date().isoformat()
        temporary_library = TemporaryDirectory()
        self.addCleanup(temporary_library.cleanup)
        self.library = Path(temporary_library.name)
        self.folder = Folder.create(root=True, name="Library", path=str(self.library))
        self.config.ALBUM_DISCOVERY.update({"target_user": self.user.name, "library_root": str(self.library)})
        self.config.BILIBILI_DISCOVERY["enabled"] = True
        self.config.RECOMMENDATION_AGENT.update({
            "provider": "openai", "daily_selection_enabled": True, "daily_model": MODEL,
        })
        self.run = BilibiliDiscoveryRun.create(
            user=self.user, source_day=self.reference.astimezone(SHANGHAI).date() - timedelta(days=1),
            status="done",
        )
        self.track_number = 0
        self._play(self._track("Repeat-listened seed"))
        paid_request = patch(
            "supysonic.recommendation_openai_daily.request_recommendation_json",
            side_effect=self._rank_in_order,
        )
        self.model = paid_request.start()
        self.addCleanup(paid_request.stop)
        fixed_clock = patch(
            "supysonic.recommendation_openai_daily.now",
            return_value=self.reference.astimezone().replace(tzinfo=None),
        )
        fixed_clock.start()
        self.addCleanup(fixed_clock.stop)

    @staticmethod
    def _rank_in_order(config, messages, schema, **kwargs):
        rows = json.loads(messages[1]["content"])["candidates"]
        return {"ranked_candidate_ids": [row["id"] for row in rows]}

    def _track(self, title):
        self.track_number += 1
        artist = Artist.create(name=f"Artist {self.track_number}")
        album = Album.create(name=f"Release {self.track_number}", artist=artist)
        path = self.library / f"private-path-{self.track_number}.flac"
        path.write_bytes(b"fixture file for the readability check")
        return Track.create(
            title=title, disc=1, number=1, duration=180, bitrate=320, last_modification=1,
            artist=artist, album=album, folder=self.folder, root_folder=self.folder,
            genre="Pop", path=str(path),
        )

    def _play(self, track):
        User_Play_Activity.create(
            user=self.user, track=track,
            time=self.reference.astimezone().replace(tzinfo=None) - timedelta(hours=1),
        )

    def _ready(self, track, kind="direct", **changes):
        values = {
            "run": self.run, "identity_key": str(track.id), "title": track.title,
            "artist": track.artist.name, "version": "studio", "track": track,
            "status": "ready", "ready_at": self.reference.replace(tzinfo=None) - timedelta(hours=1),
            "source_evidence_json": json.dumps([{
                "kind": kind, "source": {"bvid": "BV_private", "author": "Private uploader"},
                "evidence": "private watch snippet", "credential": "private token",
            }]),
        }
        values.update(changes)
        return BilibiliDiscoveryCandidate.create(**values)

    def _pool(self, baseline, capacity=3, user=None):
        user = user or self.user
        return build_bilibili_daily_pool(
            self.config, user, baseline, capacity, at=self.reference,
            listened_ids=build_recommendation_play_weights(user),
            preferences=get_recommendation_feedback_preferences(user),
        )

    def _rank(self, baseline, requested=1, pool=None):
        return select_openai_daily_tracks(
            self.config, self.user, baseline, requested, self.day, bilibili_pool=pool,
        )

    def _context(self):
        return json.loads(self.model.call_args.args[1][1]["content"])

    def test_only_current_ready_recordings_from_the_target_library_are_merged(self):
        baseline = self._track("Baseline")
        eligible = self._track("Eligible")
        self._ready(eligible)
        self._ready(self._track("Waiting"), status="waiting_album")
        self._ready(self._track("Expired"), ready_at=self.reference.replace(tzinfo=None) - timedelta(days=14))
        self._ready(self._track("Mismatched version"), version="live")
        self._ready(self._track("Unlinked Apple song"), apple_song_id="123")
        self._ready(self._track("Malformed evidence"), source_evidence_json="{}")
        self._ready(self._track("Missing source observation"), source_evidence_json='[{"kind":"direct"}]')
        heard = self._track("Played")
        self._ready(heard)
        self._play(heard)
        disliked = self._track("Disliked")
        self._ready(disliked)
        set_recommendation_feedback(self.user, str(disliked.id), "dislike")
        missing_file = self._track("Unavailable")
        self._ready(missing_file)
        Path(missing_file.path).unlink()

        pool = self._pool([baseline], capacity=30)
        self.assertEqual(pool.added_ids, (str(eligible.id),))
        self.assertIsNone(self._pool([baseline], user=User.get(User.name == "bob")))
        self.config.BILIBILI_DISCOVERY["enabled"] = False
        self.assertIsNone(self._pool([baseline]))

    def test_sol_uses_private_safe_provenance_and_source_specific_cache(self):
        self.config.RECOMMENDATION_AGENT["daily_model"] = PROFILE_MODEL
        source = self._track("Source recording")
        row = self._ready(source)
        self.assertEqual(_track_ids(self._rank([source], pool=self._pool([source]))), (str(source.id),))
        source_context = self._context()
        self.assertEqual(
            source_context["candidates"][0]["bilibiliSource"], {"kinds": ["direct"], "ageDays": 1},
        )
        self.assertIn("listeningHistory", source_context)
        self.assertIn("album", source_context["candidates"][0])
        messages = str(self.model.call_args.args[1])
        for private in ("BV_private", "Private uploader", "private watch snippet", "private-path-", "private token"):
            self.assertNotIn(private, messages)
        self._rank([source], pool=self._pool([source]))
        self.model.assert_called_once()

        entries = json.loads(row.source_evidence_json)
        entries[0]["kind"] = "style"
        row.source_evidence_json = json.dumps(entries)
        row.save()
        self._rank([source], pool=self._pool([source]))
        self.assertEqual(self._context()["candidates"][0]["bilibiliSource"]["kinds"], ["style"])
        self._rank([source])
        self.assertNotIn("bilibiliSource", self._context()["candidates"][0])
        self.assertEqual(list(self._context())[-3:], [
            "candidates", "weightedSemanticPreferences", "listeningHistory",
        ])
        self.assertEqual(self.model.call_count, 3)

    def test_luna_oversize_enhancement_uses_original_context_before_any_request(self):
        track = self._track("Baseline")
        self._ready(track)
        pool = self._pool([track])
        original = _daily_context(self.user, [track], 1, self.day, set(), 0, model=MODEL)
        byte_limit = len(_encode_context(original).encode("utf-8"))
        with patch("supysonic.recommendation_openai_daily.DAILY_CONTEXT_MAX_BYTES", byte_limit):
            self.assertEqual(_track_ids(self._rank([track], pool=pool)), (str(track.id),))
            self.assertEqual(self._context(), original)
            self.assertEqual(_track_ids(self._rank([track])), (str(track.id),))
        self.model.assert_called_once()

    def test_model_refusal_preserves_original_pool_and_existing_same_day_playlist(self):
        baseline = [self._track(f"Ordinary {index}") for index in range(3)]
        extra = self._track("Preferred by Bilibili evidence")
        self._ready(extra)
        self.model.side_effect = OpenAIRecommendationError("ranking refused", reason="invalid_response")
        with patch("supysonic.recommend._buildRecommendedTracks", return_value=baseline) as local:
            self.assertEqual(create_recommend_playlist(1, self.user, self.day, config=self.config), 1)
            before = getRecommendedPlaylistForDay(self.user, self.day)
            self.assertEqual(create_recommend_playlist(1, self.user, self.day, config=self.config), 0)
            after = getRecommendedPlaylistForDay(self.user, self.day)
            local.assert_called_once()
        self.assertEqual([row["title"] for row in self._context()["candidates"]], [
            baseline[0].title, baseline[1].title, extra.title,
        ])
        self.assertEqual(before.id, after.id)
        self.assertEqual(before.tracks, str(baseline[0].id))
        self.assertEqual(before.tracks, after.tracks)
        self.model.assert_called_once()

    def test_enhanced_invalid_cooldown_does_not_block_original_pool(self):
        track = self._track("Baseline")
        self._ready(track)
        self.model.side_effect = lambda *args, **kwargs: {"ranked_candidate_ids": [999]}
        for _ in range(2):
            with self.assertRaises(OpenAIRecommendationError):
                self._rank([track], pool=self._pool([track]))
        self.model.assert_called_once()
        self.model.side_effect = self._rank_in_order
        self.assertEqual(_track_ids(self._rank([track])), (str(track.id),))
        self.assertEqual(self.model.call_count, 2)

    def test_model_can_select_source_track_without_a_legacy_placement(self):
        baseline = [self._track(f"Ordinary {index}") for index in range(3)]
        extra = self._track("Model-selected source recording")
        self._ready(extra)
        self.model.side_effect = None
        self.model.return_value = {"ranked_candidate_ids": [3, 2, 1]}
        with patch("supysonic.recommend._buildRecommendedTracks", return_value=baseline):
            self.assertEqual(create_recommend_playlist(1, self.user, self.day, config=self.config), 1)
        playlist = getRecommendedPlaylistForDay(self.user, self.day)
        self.assertEqual(playlist.tracks, str(extra.id))
        self.assertEqual(AlbumDiscoverySelection.select().count(), 0)
        self.assertEqual(AlbumDiscoveryPlacement.select().count(), 0)
        self.model.assert_called_once()

    def test_local_daily_generation_does_not_read_the_bilibili_source(self):
        self.config.RECOMMENDATION_AGENT["daily_selection_enabled"] = False
        self._ready(self._track("Source recording"))
        with patch("supysonic.recommend.build_bilibili_daily_pool") as source:
            self.assertEqual(create_recommend_playlist(1, self.user, self.day, config=self.config), 1)
            source.assert_not_called()
        self.model.assert_not_called()

    def test_unavailable_source_file_keeps_the_ordinary_ai_daily_path(self):
        self.config.RECOMMENDATION_AGENT["daily_model"] = PROFILE_MODEL
        baseline = self._track("Baseline")
        self._ready(self._track("Source recording with unreadable backing file"))
        with patch("supysonic.recommend._buildRecommendedTracks", return_value=[baseline]), patch(
            "supysonic.bilibili_discovery.matcher.playable_local_track",
            side_effect=OSError("fixture I/O error"),
        ):
            self.assertEqual(create_recommend_playlist(1, self.user, self.day, config=self.config), 1)
        playlist = getRecommendedPlaylistForDay(self.user, self.day)
        self.assertEqual(playlist.tracks, str(baseline.id))
        self.assertNotIn("bilibiliSource", self._context()["candidates"][0])
        self.model.assert_called_once()

    def test_source_added_track_is_rechecked_after_model_request(self):
        baseline = self._track("Baseline")
        for failure in ("evidence", "file", "version", "feedback", "play"):
            with self.subTest(failure=failure):
                extra = self._track(f"Source recording {failure}")
                row = self._ready(extra)

                def rank_and_invalidate(*args, **kwargs):
                    if failure == "evidence":
                        row.status = "waiting_album"
                        row.save()
                    elif failure == "file":
                        Path(extra.path).unlink()
                    elif failure == "version":
                        Track.update(title=extra.title + " (Live)").where(Track.id == extra.id).execute()
                    elif failure == "feedback":
                        set_recommendation_feedback(self.user, str(extra.id), "dislike")
                    else:
                        self._play(extra)
                    return {"ranked_candidate_ids": [2, 1]}

                self.model.side_effect = rank_and_invalidate
                pool = self._pool([baseline])
                self.assertEqual(pool.added_ids, (str(extra.id),))
                self.assertEqual(_track_ids(self._rank([baseline], pool=pool)), (str(baseline.id),))
        self.assertEqual(self.model.call_count, 5)

    def test_full_model_pool_can_select_zero_bilibili_added_tracks(self):
        baseline = [self._track(f"Ordinary {index}") for index in range(150)]
        for track in (baseline[0], baseline[-1], baseline[-2]):
            self._ready(track)
        self._ready(self._track("New named recording"))
        self._ready(self._track("New style recording"), kind="style")
        pool = self._pool(baseline, 150)
        tracks = self._rank(baseline, 50, pool=pool)
        self.assertEqual(len(tracks), 50)
        self.assertEqual(len(self._context()["candidates"]), 150)
        self.assertEqual(len(pool.added_ids), 2)
        self.assertFalse(set(pool.added_ids).intersection(_track_ids(tracks)))
