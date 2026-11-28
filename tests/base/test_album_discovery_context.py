from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch

from supysonic.album_discovery_catalog import CatalogAlbum, CatalogTrack
from supysonic.album_discovery_context import build_discovery_listening_context
from supysonic.album_discovery_quota import SHANGHAI
from supysonic.db import (
    Album, AlbumDiscoveryRelease, AlbumDiscoverySelection, Artist, Folder, Playlist,
    StarredTrack, Track, User, User_Play_Activity, db,
)
from supysonic.recommend import RECOMMENDED_PLAYLIST_COMMENT, getRecommendPlaylistName
from supysonic.recommendation_feedback import set_recommendation_feedback

from ..testbase import TestBase


REFERENCE = datetime(2026, 11, 28, 12, tzinfo=SHANGHAI)


def local_time(value):
    return value.astimezone().replace(tzinfo=None)


class AlbumDiscoveryListeningContextTestCase(TestBase):
    def setUp(self):
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.other = User.get(User.name == "bob")
        self.folder = Folder.create(root=True, name="Music", path="/music")
        self.serial = 0

    def track(self, title, artist, album="Index title", genre="K Pop", number=1, duration=180):
        self.serial += 1
        artist, _ = Artist.get_or_create(name=artist)
        album, _ = Album.get_or_create(name=album, artist=artist)
        return Track.create(title=title, artist=artist, album=album, genre=genre, number=number,
                            disc=1, duration=duration, bitrate=320, path=f"/music/{self.serial}.flac",
                            last_modification=1, root_folder=self.folder, folder=self.folder)

    def play(self, track, days, user=None, shift=0):
        for offset in days:
            User_Play_Activity.create(
                track=track, user=user or self.user,
                time=local_time(REFERENCE - timedelta(days=offset, minutes=shift)),
            )

    def test_windows_cross_day_history_aliases_feedback_and_nonoverlapping_baseline(self):
        first = self.track("First", "Alias")
        first.artist.real_artist = Artist.create(name="Actual singer")
        first.artist.save()
        missing_tag = self.track("Other", "Second", genre=None)
        hidden = self.track("Excluded", "Hidden", genre="国语流行")
        self.play(first, [1, 1, 7, 9, 42, 90])
        self.play(first, [90], shift=1)
        self.play(first, [-1])
        self.play(first, [2], self.other)
        self.play(missing_tag, [1, 3])
        self.play(hidden, [3])
        StarredTrack.create(user=self.user, starred=first)
        set_recommendation_feedback(self.user, "Mandopop", "not_this_style")
        seed = SimpleNamespace(track_id=str(first.id), artist="Actual singer")
        context = build_discovery_listening_context(self.user, REFERENCE, (seed,), ())
        history = context["listening"]
        self.assertEqual([x["plays"] for x in history["coverage"]], [5, 6, 8, 1])
        self.assertEqual((history["submittedRowsRead90"], history["excludedOrUnindexedRows90"]), (9, 1))
        self.assertEqual([x["plays"] for x in history["withGenre"]], [3, 4, 6, 1])
        self.assertEqual(history["genreTrends"], [
            {"name": "K-Pop", "plays": [3, 4, 6, 1],
             "distinctTracks": [1, 1, 1, 1], "playDays": [2, 3, 5, 1]},
        ])
        self.assertEqual(history["tracks"][0]["artist"], "Actual singer")
        self.assertTrue(history["tracks"][0]["starred"])
        self.assertTrue(history["tracks"][0]["seed"])
        self.assertFalse(history["tracks"][1]["starred"])
        self.assertNotIn("Excluded", json.dumps(context))

    def test_empty_and_unchanged_observations_keep_same_day_inputs_stable(self):
        first = self.track("Stored", "First")
        self.play(first, [2])
        original = build_discovery_listening_context(self.user, REFERENCE, (), ())
        self.assertEqual(original, build_discovery_listening_context(
            self.user, REFERENCE + timedelta(hours=1), (), (),
        ))
        empty = build_discovery_listening_context(self.other, REFERENCE, (), ())
        self.assertEqual(empty["listening"]["tracks"], [])
        self.assertEqual(empty["dailyExposure"]["observedPlaylistDates"], [])

    def test_many_songs_share_batched_queries_and_caps_are_visible(self):
        for number in range(12):
            track = self.track("Song " + str(number), "Singer " + str(number))
            self.play(track, [1, 2])
        with patch.object(db.obj, "execute_sql", wraps=db.obj.execute_sql) as execute:
            full = build_discovery_listening_context(self.user, REFERENCE, (), ())
        self.assertEqual(full["listening"]["coverage"][2]["distinctTracks"], 12)
        self.assertLessEqual(execute.call_count, 7)
        with patch("supysonic.album_discovery_context._EVENT_LIMIT", 10), patch(
            "supysonic.album_discovery_context._TRACK_DETAIL_LIMIT", 2,
        ):
            limited = build_discovery_listening_context(self.user, REFERENCE, (), ())["listening"]
        self.assertTrue(limited["activityReadLimitReached"])
        self.assertEqual(limited["coverage"][2]["plays"], 10)
        self.assertEqual(limited["trackDetailsOmitted"], 8)

    def test_playlist_exposure_matches_versions_and_index_is_separate_from_delivery(self):
        first = self.track("Opening", "Right")
        self.track("Encore", "Wrong", number=2)
        live = self.track("Opening", "Right", album="Live")
        wrong_duration = self.track("Encore", "Right", number=2, duration=214)
        release = CatalogAlbum("200", "Index title", "Right", "https://music.apple.com/cn/album/200", (
            CatalogTrack("201", "Opening", "Right", 1, 1, 180070),
            CatalogTrack("202", "Encore", "Right", 1, 2, 180080),
        ))
        prop = SimpleNamespace(album=release, track=release.tracks[0])
        day = REFERENCE.date()
        for offset, owner, ids in (
            (1, self.user, [first, first, live, wrong_duration]), (2, self.user, [live]),
            (-1, self.user, [first]), (31, self.user, [first]), (3, self.other, [first]),
        ):
            playlist_day = day - timedelta(days=offset)
            Playlist.create(user=owner, name=getRecommendPlaylistName(owner, playlist_day.isoformat()),
                            comment=RECOMMENDED_PLAYLIST_COMMENT, created=local_time(REFERENCE - timedelta(days=4)),
                            tracks=",".join(str(t.id) for t in ids))
        context = build_discovery_listening_context(self.user, REFERENCE, (), (prop,))
        exposure = context["dailyExposure"]
        self.assertEqual(len(exposure["observedPlaylistDates"]), 2)
        self.assertEqual(exposure["candidates"], [
            {"candidate": 1, "artistOccurrences": 4, "artistPlaylistDays": 2, "matchingSongPlaylistDays": 1},
        ])
        item = context["catalog"]["releases"][0]
        self.assertEqual(item["totalPlayable"], 2)
        self.assertEqual(item["tracklist"][1], [1, 2, "Encore", "Right"])
        self.assertEqual(item["indexEditionsByAlbumArtistAndTitle"], 1)
        self.assertEqual(item["maxIndexSlotMatchesInOneEdition"], 1)
        self.assertIsNone(item["lastSameReleaseReceiptCheck"])

        checked = local_time(REFERENCE - timedelta(hours=1))
        evidence = {"tracks": [{"song_id": t.song_id, "disc_number": 1, "track_number": t.track_number,
                                "status": "verified" if t.track_number == 1 else "missing",
                                "track_id": str(first.id) if t.track_number == 1 else None}
                               for t in release.tracks]}
        stored = AlbumDiscoveryRelease.create(library_key="main", album_id="200", album_url=release.url,
                                              title=release.title, artist=release.artist, expected_tracks="{}",
                                              reconciliation=json.dumps(evidence), checked_at=checked)
        for owner in (self.other, self.user):
            AlbumDiscoverySelection.create(release=stored, user=owner, representative_song_id="201",
                                          evidence="[]", priority=1)
            item = build_discovery_listening_context(self.user, REFERENCE, (), (prop,))["catalog"]["releases"][0]
            if owner == self.other:
                self.assertIsNone(item["lastSameReleaseReceiptCheck"])
            else:
                self.assertEqual(item["lastSameReleaseReceiptCheck"]["verifiedTracks"], 1)
        other_library = build_discovery_listening_context(self.user, REFERENCE, (), (prop,), library_key="other")
        self.assertIsNone(other_library["catalog"]["releases"][0]["lastSameReleaseReceiptCheck"])
