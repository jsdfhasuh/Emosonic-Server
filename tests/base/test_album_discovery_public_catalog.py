import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from requests import Response

from supysonic.album_discovery_catalog import AppleMusicCatalog, CatalogError
from supysonic.album_discovery_public_catalog import PublicAppleMusicCatalog
from supysonic.album_discovery_service import AlbumDiscoveryService


CN = "https://music.apple.com/cn/"


def descriptor(kind, identifier, url=None):
    return {"contentDescriptor": {
        "kind": kind, "identifiers": {"storeAdamID": identifier},
        "url": url or CN + kind + "/example/" + identifier,
    }}


def artist_link(name, identifier):
    return {"title": name, "segue": {"destination": descriptor("artist", identifier)}}


def response(url, sections, schema=None, schema_id="schema:music-album", status=200, redirect=None):
    result = Response()
    result.status_code = status
    result._content_consumed = True
    if redirect:
        result.headers["Location"] = redirect
    model = {"data": [{"data": {"canonicalURL": url, "sections": [
        {"id": key, "items": value} for key, value in sections.items()
    ]}}]}
    result._content = (
        '<script id="serialized-server-data" type="application/json">' + json.dumps(model) + "</script>"
        + '<script id="' + schema_id + '" type="application/ld+json">' + json.dumps(schema or {}) + "</script>"
    ).encode("utf-8")
    return result


def track(song_id, track_number, disc=1):
    recording = descriptor("song", song_id, CN + "album/song/900?i=" + song_id)
    return {**recording, "title": "Song " + song_id, "discNumber": disc, "trackNumber": track_number,
            "artistName": "Singer", "duration": 213408,
            "isDisabled": False, "playAction": {"$kind": "playAction", "items": [recording]}}


def album_response(tracks, count):
    return response(CN + "album/test/900", {
        "track-list - 900": tracks,
        "track-list-section - 900": [{"description": f"2026年。\n{count} 首歌曲、12 分钟"}],
    }, {"@type": "MusicAlbum", "url": CN + "album/test/900", "name": "Release",
        "byArtist": [{"name": "Singer", "url": CN + "artist/singer/10"}]})


class PublicAppleMusicCatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.catalog = PublicAppleMusicCatalog(session=self.session)

    def test_public_release_uses_cn_play_actions_and_counts_regional_gaps(self):
        rows = [track("111", 1), track("112", 2), track("113", 8), track("114", 1, 2)]
        rows[1].update(isDisabled=True, playAction=None)
        self.session.get.side_effect = [
            response("", {}, status=301, redirect=CN + "album/test/900"),
            album_response(rows, 3), album_response(rows, 4),
        ]
        release = self.catalog.get_release("900")
        self.assertEqual([(t.song_id, t.disc_number, t.track_number, t.duration_ms) for t in release.tracks],
                         [("111", 1, 1, 213408), ("113", 1, 8, 213408), ("114", 2, 1, 213408)])
        self.assertIs(release, self.catalog.get_release("900"))
        self.assertIsNone(self.catalog.get_release("900", refresh=True))
        self.assertTrue(all("Authorization" not in call.kwargs["headers"]
                            and not call.kwargs["allow_redirects"] for call in self.session.get.call_args_list))

    def test_rejects_foreign_storefront_and_mismatched_play_action(self):
        wrong = track("111", 1)
        wrong["playAction"]["items"] = [descriptor("song", "777")]
        self.session.get.side_effect = [
            response("", {}, status=302, redirect="https://music.apple.com/hk/album/900"),
            album_response([wrong], 1),
            response("https://music.apple.com/us/album/900", {}),
        ]
        self.assertIsNone(self.catalog.get_release("900"))
        self.assertEqual(self.session.get.call_count, 1)
        self.assertIsNone(self.catalog.get_release("900", refresh=True))
        with self.assertRaises(CatalogError):
            self.catalog.get_release("900", refresh=True)

    def test_similar_shelf_only_accepts_identified_cn_artists(self):
        self.session.get.return_value = response(CN + "artist/singer/10", {
            "artists_10_default.marker.SimilarArtists-BubbleLockupSection": [
                {"title": "Other", **descriptor("artist", "11")},
                {"title": "Another market", **descriptor("artist", "12", "https://music.apple.com/hk/artist/12")},
                {"title": "Name without identifier"},
            ],
            "artists_10_default.marker.AppearsOn-SquareLockupSection": [
                {"title": "Unrelated", **descriptor("artist", "999")},
            ],
        })
        self.assertEqual([(a.artist_id, a.name, a.relation) for a in self.catalog.similar_artists("10")],
                         [("11", "Other", "similar")])

    def test_primary_recording_credit_required_for_cooperation(self):
        songs = [{"title": title, **descriptor("song", identifier)} for title, identifier in (
            ("Duet", "101"), ("Uncredited (feat. Other)", "102"), ("Another artist's recording", "103"),
        )]
        singer = artist_link("Singer", "10")
        other = artist_link("Other", "11")
        third = artist_link("Another", "12")
        responses = [response(CN + "artist/singer/10", {
            "artists_10_default.marker.shelfgroup.FeaturedContent.TopSongs-ArtistFeaturedContentAndTracks":
                [{"tracks": songs}],
            "artists_10_default.marker.RecommendedArtists-BubbleLockupSection": [descriptor("artist", "999")],
        })]
        for item, credits in zip(songs, ([singer, other], [singer], [other, third])):
            song_id = item["contentDescriptor"]["identifiers"]["storeAdamID"]
            responses.append(response(CN + "song/test/" + song_id, {
                "songDetail": [{**descriptor("song", song_id), "title": item["title"], "artistLinks": credits}],
                "composer-and-lyrics": [third],
            }))
        self.session.get.side_effect = responses
        self.assertFalse(self.catalog.similar_artists("10"))
        artists = self.catalog.collaborating_artists("10")
        self.assertEqual([(a.artist_id, a.name, a.relation, a.source_song_id, a.source_song_title)
                          for a in artists], [("11", "Other", "collaborator", "101", "Duet")])

    def test_search_uses_playable_album_metadata_and_exact_song_credits(self):
        song_id = "111"
        self.session.get.side_effect = [
            response(CN + "search?term=Song+111+Singer", {"track-section - song": [
                {"title": "Song 111", **descriptor("song", song_id)},
                {"title": "Song 111 (Live)", **descriptor("song", "999")},
            ]}),
            response(CN + "song/111", {"songDetail": [{
                "title": "Song 111", **descriptor("song", song_id), "artistLinks": [artist_link("Singer", "10")],
            }]}, {"audio": {"@type": "MusicRecording", "url": CN + "song/111",
                            "inAlbum": {"url": CN + "album/900"}, "genre": ["Pop"]}}, schema_id="schema:song"),
            album_response([track("111", 1)], 1),
        ]
        songs = self.catalog.search_seed_songs("Song 111", "Singer")
        self.assertEqual(len(songs), 1)
        self.assertEqual((songs[0].album_name, songs[0].artist_ids, songs[0].album_ids, songs[0].genres),
                         ("Release", ("10",), ("900",), ("Pop",)))
        self.assertEqual(songs[0].track.song_id, song_id)
        self.assertFalse(self.catalog.songs_by_isrc("USABC1200001"))
        self.assertEqual(self.session.get.call_count, 3)

    def test_from_config_selects_public_or_authorized_catalog_only_when_needed(self):
        config = {"ALBUM_DISCOVERY": {"target_user": "alice", "library_root": tempfile.gettempdir()}}
        with patch("supysonic.album_discovery_service.load_album_discovery_apple_token",
                   side_effect=[None, "test-token"]) as token:
            self.assertIsNone(AlbumDiscoveryService.from_config(config, with_catalog=False).catalog)
            token.assert_not_called()
            self.assertIsInstance(AlbumDiscoveryService.from_config(config).catalog, PublicAppleMusicCatalog)
            self.assertIsInstance(AlbumDiscoveryService.from_config(config).catalog, AppleMusicCatalog)
