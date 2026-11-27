import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch

from supysonic.album_discovery_candidates import (
    AlbumDiscoveryCandidates,
    _find_release,
    album_discovery_candidate_allowed,
    discover_album_candidates,
    select_discovery_seeds,
)
from supysonic.album_discovery_catalog import (
    AppleMusicCatalog,
    CatalogAlbum,
    CatalogArtist,
    CatalogError,
    CatalogSong,
    CatalogTrack,
)
from supysonic.db import (
    Album, Artist, Folder, StarredTrack, Track, TrackArtist, User, User_Play_Activity,
)
from supysonic.openai_recommendations import OpenAIRecommendationError
from supysonic.recommendation_agent_cache import clear_recommendation_agent_cache
from supysonic.recommendation_feedback import set_recommendation_feedback

from ..testbase import TestBase


CN = "/v1/catalog/cn/"
API = "https://api.music.apple.com"
MUSIC = "https://music.apple.com/cn/album/"
REFERENCE = datetime(2026, 11, 26, 17, tzinfo=timezone.utc)
ISRC = "USABC1200001"


def resource_song(
    song_id: str, number: int, *, disc: int = 1, playable: bool = True,
    storefront: str = "cn",
) -> dict:
    attributes = {
        "name": "Song " + song_id, "artistName": "Catalog Artist",
        "discNumber": disc, "trackNumber": number, "durationInMillis": 213408,
        "url": f"https://music.apple.com/{storefront}/album/example/900?i={song_id}",
        "isrc": ISRC,
    }
    if playable:
        attributes["playParams"] = {"kind": "song", "id": song_id}
    return {
        "type": "songs", "id": song_id,
        "href": f"/v1/catalog/{storefront}/songs/{song_id}",
        "attributes": attributes,
    }


def catalog_release(identifier: str, title: str, *tracks, **metadata) -> CatalogAlbum:
    return CatalogAlbum(identifier, title, "Catalog Artist", MUSIC + identifier, tracks, **metadata)


def catalog_song(song_id: str, title: str, artist: str, album: str, album_id: str,
                 artist_id: str, **fields) -> CatalogSong:
    track = CatalogTrack(
        song_id, title, artist, 1, fields.get("number", 1), fields.get("duration", 180000),
        fields.get("isrc"),
    )
    return CatalogSong(track, album, (album_id,), (artist_id,), ("Pop",))


class AppleMusicCatalogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.session = Mock()
        self.catalog = AppleMusicCatalog("test-developer-token", session=self.session)

    @staticmethod
    def response(payload: dict, status: int = 200):
        response = Mock(status_code=status)
        response.json.return_value = payload
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        return context

    def release_payload(self, data: list, **fields) -> dict:
        album = {
            "type": "albums", "id": "900", "href": CN + "albums/900",
            "attributes": {
                "name": "Verified Release", "artistName": "Catalog Artist",
                "url": MUSIC + "verified/900?i=999", "trackCount": 5,
            },
            "relationships": {"tracks": {"data": data, **fields}},
        }
        return {"data": [album]}

    def test_paginates_multidisc_retaining_cn_playable_number_gaps(self) -> None:
        cursor = CN + "albums/900/tracks?offset=3"
        self.session.get.side_effect = [
            self.response(self.release_payload([
                resource_song("111", 1), resource_song("112", 2, playable=False),
                {"id": "500", "type": "music-videos"},
            ], next=cursor)),
            self.response({"data": [
                resource_song("113", 3), resource_song("114", 1, disc=2),
            ]}),
        ]
        release = self.catalog.get_release("900")
        self.assertEqual(release.url, MUSIC + "verified/900")
        self.assertEqual(
            [(track.song_id, track.disc_number, track.track_number) for track in release.tracks],
            [("111", 1, 1), ("113", 1, 3), ("114", 2, 1)],
        )
        self.assertEqual(release, CatalogAlbum.from_payload(json.loads(json.dumps(release.to_payload()))))
        self.assertIs(release, self.catalog.get_release("900"))
        calls = self.session.get.call_args_list
        self.assertEqual([call.args[0] for call in calls], [API + CN + "albums/900", API + cursor])
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in calls))

    def test_rejects_foreign_cursor_foreign_album_link_and_untrusted_redirect(self) -> None:
        self.session.get.side_effect = [
            self.response(self.release_payload(
                [resource_song("111", 1)], next="/v1/catalog/hk/albums/900/tracks?offset=1",
            )),
        ]
        with self.assertRaises(CatalogError):
            self.catalog.get_release("900")
        self.assertEqual(self.session.get.call_count, 1)

        payload = self.release_payload([resource_song("111", 1)])
        payload["data"][0]["attributes"]["url"] = "https://music.apple.com/hk/album/example/900"
        self.session.get.side_effect = [self.response(payload), self.response({}, status=302)]
        self.assertIsNone(self.catalog.get_release("900", refresh=True))
        with self.assertRaises(CatalogError):
            self.catalog.get_release("900", refresh=True)

    def test_playable_outside_cn_or_conflicting_position_rejects_release(self) -> None:
        self.session.get.side_effect = [
            self.response(self.release_payload([resource_song("111", 1, storefront="hk")])),
            self.response(self.release_payload([resource_song("111", 1), resource_song("112", 1)])),
        ]
        self.assertIsNone(self.catalog.get_release("900"))
        self.assertIsNone(self.catalog.get_release("900", refresh=True))


class AlbumDiscoveryCandidatesTestCase(TestBase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.get(User.name == "alice")
        self.folder = Folder.create(root=True, name="Music", path="/music")
        self.catalog = Mock(spec=AppleMusicCatalog)
        self.catalog.other_versions.return_value = ()
        self.catalog.songs_by_isrc.return_value = ()
        self.number = 0

    def track(self, artist: str, title: str, album: str = "Local Album") -> Track:
        self.number += 1
        artist, _ = Artist.get_or_create(name=artist)
        album, _ = Album.get_or_create(name=album, artist=artist)
        return Track.create(
            title=title, disc=1, number=self.number, duration=180, artist=artist, album=album,
            genre="Pop", bitrate=320, path=f"/music/seed-{self.number}.flac",
            last_modification=1, root_folder=self.folder, folder=self.folder,
        )

    def play(self, track: Track, *offsets: int) -> None:
        for hours in offsets:
            observed = (REFERENCE - timedelta(hours=hours)).astimezone().replace(tzinfo=None)
            User_Play_Activity.create(track=track, user=self.user, time=observed)

    def test_shanghai_calendar_collapses_same_day_and_ranks_current_favorites_first(self) -> None:
        favorite = self.track("Favourite Artist", "Cross Midnight")
        ordinary = self.track("Ordinary Artist", "Three Days")
        hidden = self.track("Not This Artist", "Hidden Song")
        loop = self.track("Loop Artist", "One Session")
        outside = self.track("Old Artist", "Outside")
        other = self.track("Favourite Artist", "Second Track")
        self.play(favorite, 0, 2, 2, 7)
        self.play(ordinary, 2, 26, 50, 74)
        self.play(hidden, 2, 26, 50)
        self.play(loop, 0, 0, 0)
        self.play(outside, 0, 24 * 14, 24 * 15)
        self.play(other, 2, 26, 50)
        StarredTrack.create(user=self.user, starred=favorite)
        StarredTrack.create(user=self.user, starred=loop)
        set_recommendation_feedback(self.user, str(hidden.artist_id), "hide_artist")

        seeds = select_discovery_seeds(self.user, now=REFERENCE)
        self.assertEqual([seed.track_id for seed in seeds], [str(favorite.id), str(ordinary.id)])
        self.assertEqual(seeds[0].play_dates, ("2026-11-26", "2026-11-27"))
        self.assertTrue(seeds[0].starred)
        self.assertEqual(len(seeds[1].play_dates), 4)

    def test_no_trusted_seed_does_not_call_catalog(self) -> None:
        loop = self.track("Loop Artist", "One Session")
        self.play(loop, 0, 0, 0)
        result = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
        self.assertEqual(result.reason, "no_seeds")
        self.assertEqual(self.catalog.mock_calls, [])

    def test_twelve_distinct_artists_include_less_frequent_genres(self) -> None:
        for number in range(12):
            track = self.track(f"Repeat {number}", f"Song {number}")
            Track.update(genre="K-Pop").where(Track.id == track.id).execute()
            self.play(track, 2, 26, 50, 74)
        track = self.track("Another Artist", "Rare Style")
        Track.update(genre="Mandopop").where(Track.id == track.id).execute()
        self.play(track, 2, 26, 50)
        seeds = select_discovery_seeds(self.user, now=REFERENCE)
        self.assertEqual(len(seeds), 12)
        self.assertEqual(len({seed.artist for seed in seeds}), 12)
        self.assertIn(str(track.id), {seed.track_id for seed in seeds})

    def test_seed_rejects_same_name_ambiguity_and_verifies_collaborations(self) -> None:
        seed = self.track("Local Singer", "Distinct Seed")
        self.play(seed, 2, 26, 50)
        first = catalog_song("800", seed.title, "Local Singer", seed.album.name, "900", "700")
        second = catalog_song("801", seed.title, "Local Singer", seed.album.name, "901", "701")
        self.catalog.search_seed_songs.return_value = (first, second)
        self.catalog.get_artists.side_effect = lambda song_id: (
            CatalogArtist("700" if song_id == "800" else "701", "Local Singer"),
        )
        result = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
        self.assertEqual(result.reason, "no_catalog_seeds")
        self.catalog.similar_artists.assert_not_called()

        other = Artist.create(name="Collaborator")
        TrackArtist.create(track_id=seed, artist_id=other, position=2)
        duet = catalog_song("802", seed.title, "Local Singer & Collaborator", seed.album.name, "902", "700")
        self.catalog.search_seed_songs.return_value = (first, duet)
        self.catalog.get_artists.side_effect = lambda song_id: (
            (CatalogArtist("700", "Local Singer"), CatalogArtist("702", "Collaborator"))
            if song_id == "802" else (CatalogArtist("700", "Local Singer"),)
        )
        self.catalog.similar_artists.return_value = ()
        result = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
        self.assertEqual(result.seeds[0].catalog_song_id, "802")

    def test_prefers_verified_standard_recording_over_deluxe_and_compilation(self) -> None:
        song = catalog_song("801", "Strong Song", "Catalog Artist", "Compilation", "900", "700", isrc=ISRC)
        std = catalog_song("802", "Strong Song", "Catalog Artist", "Original", "901", "700", isrc=ISRC)
        live = catalog_song("803", "Strong Song (Live)", "Catalog Artist", "Live", "902", "700", isrc=ISRC)
        deluxe = catalog_song("804", "Strong Song", "Catalog Artist", "Original (Deluxe)", "903", "700", isrc=ISRC)
        releases = {
            "900": catalog_release("900", "Compilation", song.track, is_compilation=True),
            "901": catalog_release("901", "Original", std.track),
            "902": catalog_release("902", "Live", live.track),
            "903": catalog_release("903", "Original (Deluxe)", deluxe.track),
        }
        self.catalog.get_release.side_effect = releases.get
        self.catalog.songs_by_isrc.return_value = (std, live, deluxe)
        self.assertEqual(_find_release(self.catalog, song), (releases["901"], std.track))
        self.assertEqual(_find_release(self.catalog, live), (releases["902"], live.track))

    def configure_discovery(self) -> None:
        first = self.track("Local Singer A", "Seed A")
        second = self.track("Local Singer B", "Seed B")
        for track in (first, second):
            self.play(track, 2, 26, 50)
        seed_songs = {
            first.title: catalog_song("101", first.title, first.artist.name, first.album.name, "200", "501"),
            second.title: catalog_song("102", second.title, second.artist.name, second.album.name, "201", "502"),
        }
        self.catalog.search_seed_songs.side_effect = lambda title, artist: (seed_songs[title],)
        artists = {
            result.track.song_id: CatalogArtist(result.artist_ids[0], result.track.artist)
            for result in seed_songs.values()
        }
        self.catalog.get_artists.side_effect = lambda identifier: (artists[identifier],)
        related = CatalogArtist("601", "Catalog Artist")
        self.catalog.similar_artists.side_effect = lambda identifier: (
            (related, CatalogArtist("602", "Another Artist")) if identifier == "501" else (related,)
        )
        song_a = catalog_song("301", "Same Album One", related.name, "Discovery One", "401", "601")
        song_b = catalog_song("302", "Same Album Two", related.name, "Discovery One", "401", "601", number=2)
        song_c = catalog_song("303", "Different Album", "Another Artist", "Discovery Two", "402", "602")
        self.catalog.top_songs.side_effect = lambda identifier: (
            (song_a, song_b) if identifier == "601" else (song_c,)
        )
        releases = {
            "401": catalog_release("401", "Discovery One", song_a.track, song_b.track),
            "402": CatalogAlbum("402", "Discovery Two", "Another Artist", MUSIC + "402", (song_c.track,)),
        }
        self.catalog.get_release.side_effect = releases.get

    @staticmethod
    def rank(config, messages, schema, **kwargs) -> dict:
        context = json.loads(messages[1]["content"])
        return {"ranking": [
            {"id": row["id"], "evidence_ids": [item["id"] for item in row["evidence"]]}
            for row in reversed(context["songs"])
        ]}

    def test_same_album_merges_sources_independently_of_quota_and_reuses_exact_rank(self) -> None:
        self.configure_discovery()
        with patch(
            "supysonic.album_discovery_candidates.request_recommendation_json", side_effect=self.rank,
        ) as model:
            first = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
            clear_recommendation_agent_cache(self.user)
            second = discover_album_candidates(
                self.config, self.user, self.catalog, now=REFERENCE + timedelta(hours=1),
            )
        self.assertEqual(first, second)
        self.assertEqual(first, AlbumDiscoveryCandidates.from_payload(
            json.loads(json.dumps(first.to_payload())),
        ))
        self.assertEqual({candidate.album.album_id for candidate in first.candidates}, {"401", "402"})
        same_album = next(candidate for candidate in first.candidates if candidate.album.album_id == "401")
        self.assertEqual(same_album.representative_song_id, "302")
        self.assertEqual({proof.seed_title for proof in same_album.evidence}, {"Seed A", "Seed B"})
        self.assertEqual({proof.song_id for proof in same_album.evidence}, {"301", "302"})
        self.assertEqual(len(same_album.evidence), 4)
        model.assert_called_once()
        self.assertEqual(model.call_args.kwargs["workload"], "album_discovery")
        prompt = json.loads(model.call_args.args[1][1]["content"])
        self.assertEqual(len(prompt["songs"]), 3)
        self.assertTrue(all("url" not in song for song in prompt["songs"]))

    def test_cooperation_evidence_survives_model_ranking_and_saved_preview(self) -> None:
        self.configure_discovery()
        self.catalog.similar_artists.side_effect = lambda _: ()
        self.catalog.collaborating_artists = lambda _: (
            CatalogArtist("601", "Catalog Artist", "collaborator", "999", "Joint single"),
        )
        with patch(
            "supysonic.album_discovery_candidates.request_recommendation_json", side_effect=self.rank,
        ) as model:
            result = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
        prompt = json.loads(model.call_args.args[1][1]["content"])
        for song in prompt["songs"]:
            for item in song["evidence"]:
                self.assertEqual(item["source"], "cnCatalogSharedRecordingCreditAndTopSong")
                self.assertEqual(item["sharedRecording"], "Joint single")
        saved = AlbumDiscoveryCandidates.from_payload(json.loads(json.dumps(result.to_payload())))
        self.assertEqual(len(saved.candidates), 1)
        for item in saved.candidates[0].evidence:
            self.assertEqual((item.relation, item.source_song_id), ("collaborator", "999"))

    def test_invalid_reasons_cannot_dispatch_or_charge_again_same_day(self) -> None:
        self.configure_discovery()
        with patch(
            "supysonic.album_discovery_candidates.request_recommendation_json",
            return_value={"ranking": [{"id": 1, "evidence_ids": [3]}]},
        ) as model:
            first = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
            second = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
        self.assertEqual(first.reason, "ranking_unavailable")
        self.assertEqual(second.reason, "ranking_unavailable")
        self.assertFalse(first.candidates or second.candidates)
        model.assert_called_once()

    def test_empty_model_and_exhausted_budget_are_safe(self) -> None:
        self.configure_discovery()
        with patch(
            "supysonic.album_discovery_candidates.request_recommendation_json",
            side_effect=[
                OpenAIRecommendationError("spending exhausted", reason="budget"),
                {"ranking": []},
            ],
        ) as model:
            first = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
            second = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
            later = discover_album_candidates(
                self.config, self.user, self.catalog, now=REFERENCE + timedelta(days=1),
            )
        self.assertEqual(first.reason, "ranking_budget")
        self.assertEqual(second.reason, "ranking_budget")
        self.assertEqual(later.reason, "no_selection")
        self.assertEqual(model.call_count, 2)

    def test_new_feedback_during_model_request_removes_candidate(self) -> None:
        self.configure_discovery()

        def feedback(*args, **kwargs):
            set_recommendation_feedback(self.user, "401", "hide_album")
            return self.rank(*args, **kwargs)

        with patch("supysonic.album_discovery_candidates.request_recommendation_json", side_effect=feedback):
            result = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
        self.assertEqual([item.album.album_id for item in result.candidates], ["402"])

    def test_saved_candidate_is_rechecked_against_current_genre_feedback(self) -> None:
        self.configure_discovery()
        with patch("supysonic.album_discovery_candidates.request_recommendation_json", side_effect=self.rank):
            result = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
        candidate = result.candidates[0]
        self.assertTrue(album_discovery_candidate_allowed(self.user, candidate))
        set_recommendation_feedback(self.user, "Pop", "not_this_style")
        self.assertFalse(album_discovery_candidate_allowed(self.user, candidate))

    def configure_editorial(self):
        tracks = [self.track("Korean Act A", "Repeat A"), self.track("Korean Act B", "Repeat B")]
        for track, genre in zip(tracks, ("K-Pop", "韩国流行乐")):
            Track.update(genre=genre).where(Track.id == track.id).execute()
            self.play(track, 2, 26, 50)
        self.catalog.search_seed_songs.return_value = ()
        song = replace(
            catalog_song("401", "Editorial Song", "Catalog Artist", "Editorial Album", "501", "601"),
            genres=("K-Pop",),
        )
        album = catalog_release("501", song.album_name, song.track)
        self.catalog.get_release.side_effect = {"501": album}.get
        self.catalog.style_songs.return_value = (song,)
        return tracks, song

    def test_editorial_style_without_catalog_seed_requires_two_artists_and_song_genre(self) -> None:
        _, song = self.configure_editorial()
        self.catalog.style_songs.return_value = (
            replace(song, genres=("Pop",)), song, replace(song, album_ids=("999",)),
        )
        with patch("supysonic.album_discovery_candidates.request_recommendation_json",
                   side_effect=self.rank) as model:
            found = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
        self.catalog.style_songs.assert_called_once_with("K-Pop")
        self.catalog.similar_artists.assert_not_called()
        self.assertEqual([c.album.album_id for c in found.candidates], ["501"])
        self.assertEqual(found.diagnostics["styles"][0]["verified"], 1)
        self.assertEqual(found.diagnostics["proposals"][0]["sources"], ["style"])
        self.assertEqual(set(found.diagnostics["proposals"][0]["seed_track_ids"]),
                         {seed.track_id for seed in found.seeds})
        self.assertEqual(found.diagnostics["ranking_count"], 1)
        proof = found.candidates[0].evidence[0]
        self.assertEqual({proof.seed_title, proof.supporting_seed_title}, {"Repeat A", "Repeat B"})
        self.assertEqual(proof.style, "K-Pop")
        self.assertIn("/cn/curator/", proof.style_source_url)
        context = json.loads(model.call_args.args[1][1]["content"])
        source = context["songs"][0]["evidence"][0]
        self.assertEqual(source["source"], "cnEditorialStyleAndSongGenre")
        self.assertNotEqual(source["seed"], source["supportingSeed"])
        self.assertEqual(AlbumDiscoveryCandidates.from_payload(found.to_payload()), found)
        self.assertTrue(album_discovery_candidate_allowed(self.user, found.candidates[0]))
        set_recommendation_feedback(self.user, proof.supporting_seed_track_id, "dislike")
        self.assertFalse(album_discovery_candidate_allowed(self.user, found.candidates[0]))

    def test_editorial_search_caps_and_one_artist_cannot_establish_a_style(self) -> None:
        for style in ("K-Pop", "Mandopop", "J-Pop", "Cantopop"):
            for variant in ("A", "B"):
                track = self.track(f"{style} Act {variant}", f"{style} {variant}")
                Track.update(genre=style).where(Track.id == track.id).execute()
                self.play(track, 2, 26, 50)
        single = self.track("Only Rock Act", "One Rock Recording")
        Track.update(genre="Rock").where(Track.id == single.id).execute()
        self.play(single, 2, 26, 50, 74)
        self.catalog.search_seed_songs.return_value = ()
        def many_songs(style):
            return tuple(replace(
                catalog_song(str(n), "Editorial " + str(n), "Artist", "Record", "700", "800"),
                genres=(style,),
            ) for n in range(1, 7))
        self.catalog.style_songs.side_effect = many_songs
        self.catalog.get_release.return_value = None
        with patch("supysonic.album_discovery_candidates.request_recommendation_json") as model:
            found = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
        self.assertEqual(found.reason, "no_candidates")
        self.assertEqual(len(found.diagnostics["styles"]), 3)
        self.assertTrue(all(s["songs"] == 3 and s["support"] == 2 for s in found.diagnostics["styles"]))
        self.assertNotIn("摇滚", [r["style"] for r in found.diagnostics["styles"]])
        self.assertEqual(self.catalog.get_release.call_count, 9)
        model.assert_not_called()

    def test_editorial_style_removed_during_ranking_and_hidden_genre_alias(self) -> None:
        self.configure_editorial()
        def rank_then_hide(*args, **kwargs):
            set_recommendation_feedback(self.user, "韩国流行乐", "not_this_style")
            return self.rank(*args, **kwargs)
        with patch("supysonic.album_discovery_candidates.request_recommendation_json",
                   side_effect=rank_then_hide) as model:
            found = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
            later = discover_album_candidates(self.config, self.user, self.catalog,
                                             now=REFERENCE + timedelta(days=1))
        self.assertEqual(found.reason, "no_selection")
        self.assertEqual(found.diagnostics["ranking_count"], 1)
        self.assertFalse(later.candidates)
        self.assertIsNone(later.diagnostics["ranking_count"])
        self.catalog.style_songs.assert_called_once()
        model.assert_called_once()

    def test_style_and_artist_sources_merge_same_recording_across_verified_versions(self) -> None:
        self.configure_discovery()
        Track.update(genre="K-Pop").where(Track.title.in_(("Seed A", "Seed B"))).execute()
        original = self.catalog.top_songs("601")[0]
        original = replace(original, track=replace(original.track, isrc=ISRC))
        edition = replace(original, track=replace(original.track, song_id="777"),
                          album_name="Discovery One (Reissue)", album_ids=("999",), genres=("K-Pop",))
        original_release = catalog_release("401", original.album_name, original.track)
        later = catalog_release("999", edition.album_name, edition.track, release_date="2025-01-01")
        releases = {"401": replace(original_release, release_date="2020-01-01"), "999": later}
        self.catalog.get_release.side_effect = releases.get
        self.catalog.top_songs.side_effect = lambda artist: (original,) if artist == "601" else ()
        self.catalog.style_songs.return_value = (edition,)
        with patch("supysonic.album_discovery_candidates.request_recommendation_json", side_effect=self.rank):
            found = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
        self.assertEqual(len(found.candidates), 1)
        chosen = found.candidates[0]
        self.assertEqual(chosen.album.album_id, "401")
        self.assertEqual({proof.song_id for proof in chosen.evidence}, {"301"})
        self.assertEqual(set(found.diagnostics["proposals"][0]["sources"]), {"similar", "style"})
        legacy = found.to_payload()
        legacy.pop("diagnostics")
        self.assertEqual(AlbumDiscoveryCandidates.from_payload(legacy).diagnostics, {})

    def test_catalog_errors_are_isolated_and_fully_empty_failures_can_retry(self) -> None:
        self.configure_discovery()
        Track.update(genre="K-Pop").where(Track.title.in_(("Seed A", "Seed B"))).execute()
        self.catalog.style_songs.side_effect = CatalogError("editorial page unavailable")
        def similar(artist_id):
            if artist_id == "501":
                raise CatalogError("artist page unavailable")
            return (CatalogArtist("601", "Catalog Artist"),)
        self.catalog.similar_artists.side_effect = similar
        with patch("supysonic.album_discovery_candidates.request_recommendation_json",
                   side_effect=self.rank) as model:
            found = discover_album_candidates(self.config, self.user, self.catalog, now=REFERENCE)
            self.catalog.search_seed_songs.side_effect = CatalogError("seed search unavailable")
            empty = discover_album_candidates(self.config, self.user, self.catalog,
                                             now=REFERENCE + timedelta(days=1))
        self.assertEqual([v.album.album_id for v in found.candidates], ["401"])
        self.assertEqual(found.diagnostics["styles"][0]["errors"], 1)
        self.assertEqual(sum(s["errors"] for s in found.diagnostics["seeds"]), 1)
        self.assertEqual(empty.reason, "catalog_unavailable")
        self.assertEqual(sum(s["errors"] for s in empty.diagnostics["seeds"]), 2)
        model.assert_called_once()
