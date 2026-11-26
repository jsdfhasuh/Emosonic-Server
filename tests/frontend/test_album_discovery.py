import tempfile
from pathlib import Path
from unittest.mock import patch

from flask import current_app

from supysonic import db
from supysonic.album_discovery_candidates import AlbumDiscoveryCandidates
from supysonic.album_discovery_service import AlbumDiscoveryService
from supysonic.album_discovery_settings import load_album_discovery_runtime
from tests.base.test_album_discovery_service import FakeCatalog, FakeOracle, candidate

from .frontendtestbase import FrontendTestBase


class AlbumDiscoveryFrontendTestCase(FrontendTestBase):
    def setUp(self) -> None:
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.item = candidate("101")
        self.oracle = FakeOracle()
        with self.app_context():
            config = current_app.config
            config["ALBUM_DISCOVERY"] = {
                "target_user": "alice", "library_root": str(Path(directory.name)),
            }
            self.service = AlbumDiscoveryService(
                config, load_album_discovery_runtime(config),
                catalog=FakeCatalog(self.item), oracle=self.oracle,
            )

    def test_authenticated_preview_and_single_album_confirmation(self) -> None:
        url = "/recommendations/discovery"
        self.assertEqual(self.client.get(url).status_code, 302)
        self._login("alice", "Alic3")
        with patch("supysonic.frontend.album_discovery.AlbumDiscoveryService.from_config",
                   return_value=self.service), patch(
                       "supysonic.album_discovery_service.discover_album_candidates",
                       return_value=AlbumDiscoveryCandidates(candidates=(self.item,)),
                   ) as discover, patch(
                       "supysonic.album_discovery_service.album_discovery_candidate_allowed",
                       return_value=True,
                   ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            discover.assert_not_called()
            self.assertEqual(self.client.post(url).status_code, 403)
            with self.client.session_transaction() as session:
                token = session["album_discovery_csrf"]
            response = self.client.post(url, data={"csrf": token})
            self.assertEqual(response.status_code, 200)
            self.assertIn("Edition 101", response.data)
            self.assertIn("Track 101", response.data)
            self.assertIn("等待收件", response.data)
            self.assertEqual(db.AlbumDiscoveryOrder.select().count(), 0)
            self.assertEqual(db.Playlist.select().count(), 0)

            preview = db.AlbumDiscoveryPreview.get()
            confirm = f"{url}/{preview.id}/confirm"
            self.assertEqual(self.client.post(confirm, data={"album_id": "101"}).status_code, 403)
            multi = {"csrf": token, "album_id": ["101", "102"]}
            self.assertEqual(self.client.post(confirm, data=multi).status_code, 400)
            self.assertEqual(
                self.client.post(confirm, data={"csrf": token, "album_id": "102"}).status_code, 303,
            )
            self.assertFalse(self.oracle.attempts)
            result = self.client.post(confirm, data={"csrf": token, "album_id": "101"})
            self.assertEqual(result.status_code, 303)
            self.assertEqual(len(self.oracle.attempts), 1)
            self.assertIn("已确认的专辑", self.client.get(url).data)

            self._logout()
            self._login("bob", "B0b")
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(confirm, data={"csrf": token, "album_id": "101"}).status_code, 404)
