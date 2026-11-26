from hashlib import md5
from tempfile import TemporaryDirectory

from PIL import Image

from supysonic.account_avatars import account_avatar_path
from supysonic.db import User
from .apitestbase import ApiTestBase


class ListeningProfileApiTests(ApiTestBase):
    def setUp(self):
        super().setUp()
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = self.client.application.config
        config["BASE"] = dict(config["BASE"], tempdatafolder=temp.name)
        self.avatar = account_avatar_path(config, User.get(User.name == "bob"))
        self.avatar.parent.mkdir(parents=True)
        with Image.new("RGB", (16, 16), "navy") as img:
            img.save(self.avatar)
        self.auth = {"u": "bob", "s": "testsalt", "t": md5(b"B0btestsalt").hexdigest(),
                     "v": "1.16.1", "c": "emosonic", "f": "json"}

    def test_image_and_conditional_get_are_account_scoped(self):
        url = "/rest/getAvatar.view"
        with self.client.get(url, query_string=self.auth) as image:
            self.assertEqual(image.mimetype, "image/png")
            self.assertEqual(image.data, self.avatar.read_bytes())
            tag = image.headers["ETag"]
        with self.client.get(url, query_string=self.auth, headers={"If-None-Match": tag}) as cached:
            self.assertEqual(cached.status_code, 304)
            self.assertEqual(cached.data, b"")
        denied = self.client.get(url, query_string=dict(self.auth, username="alice"))
        self.assertEqual(denied.json["subsonic-response"]["error"]["code"], 50)

    def test_summary_period_and_empty_account(self):
        for period in ("7", "30", "all"):
            response = self.client.get("/rest/getEmoListeningProfile.view", query_string=dict(self.auth, period=period))
            profile = response.json["subsonic-response"]["listeningProfile"]
            self.assertEqual((profile["period"], profile["playCount"], profile["trackCount"]), (period, 0, 0))
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        invalid = self.client.get("/rest/getEmoListeningProfile", query_string=dict(self.auth, period="invalid"))
        self.assertEqual(invalid.json["subsonic-response"]["status"], "failed")
