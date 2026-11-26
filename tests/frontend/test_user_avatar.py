import io

from PIL import Image, PngImagePlugin

from supysonic.db import User
from supysonic.account_avatars import AVATAR_SIZE

from .frontendtestbase import FrontendTestBase


class UserAvatarTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        flask_config = self.client.application.config
        flask_config["BASE"] = {
            **flask_config["BASE"],
            "tempdatafolder": flask_config["WEBAPP"]["cache_dir"],
        }

    def _token(self):
        self.assertEqual(self.client.get("/user/me").status_code, 200)
        with self.client.session_transaction() as session:
            return session["profile_avatar_csrf"]["value"]

    def _png(self, size=(96, 48)):
        data = io.BytesIO()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Comment", "Strip this private information")
        with Image.new("RGB", size, color=(30, 60, 90)) as image:
            image.save(data, format="PNG", pnginfo=metadata)
        data.seek(0)
        return data

    def _upload(self, token, image, path="/user/me/avatar"):
        return self.client.post(
            path, data={"csrf_token": token, "avatar": (image, "avatar.png")},
            content_type="multipart/form-data",
        )

    def _image_bytes(self, path="/user/me/avatar"):
        # The standard test wrapper decodes response bodies as text.
        with self.client.open(path, method="GET") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/png")
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
            return response.get_data()

    def test_upload_reencodes_and_remove_restores_default(self):
        self._login("bob", "B0b")
        token = self._token()
        self.assertEqual(self.client.get("/user/me/avatar").status_code, 404)

        upload = self._upload(token, self._png())
        self.assertEqual(upload.status_code, 303)
        self.assertEqual(upload.location, "/user/me")
        with Image.open(io.BytesIO(self._image_bytes())) as image:
            self.assertEqual(image.size, AVATAR_SIZE)
            self.assertNotIn("Comment", image.info)
            self.assertEqual(image.getpixel((0, 0)), (30, 60, 90, 255))

        remove = self.client.post("/user/me/avatar/remove", data={"csrf_token": token})
        self.assertEqual(remove.status_code, 303)
        self.assertEqual(self.client.get("/user/me/avatar").status_code, 404)

    def test_invalid_image_leaves_uploaded_avatar_intact(self):
        self._login("bob", "B0b")
        token = self._token()
        self.assertEqual(self._upload(token, self._png()).status_code, 303)
        previous = self._image_bytes()

        rejected = self._upload(token, io.BytesIO(b"not an image"), path="/user/me/avatar?period=30")
        self.assertEqual(rejected.status_code, 303)
        self.assertEqual(rejected.location, "/user/me?period=30&avatar=1")
        self.assertEqual(self._upload(token, self._png(size=(4097, 1))).status_code, 303)
        self.assertEqual(self._image_bytes(), previous)

    def test_only_current_account_can_write_and_auth_is_required(self):
        alice = User.get(User.name == "alice")
        self.assertEqual(self.client.get("/user/me/avatar").status_code, 302)

        self._login("alice", "Alic3")
        self.assertEqual(self._upload(self._token(), self._png()).status_code, 303)
        self._logout()

        self._login("bob", "B0b")
        token = self._token()
        self.assertEqual(self._upload("wrong", self._png()).status_code, 403)
        self.assertEqual(self.client.post("/user/me/avatar/remove").status_code, 403)
        path = f"/user/{alice.id}/avatar"
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self._upload(token, self._png(), path=path).status_code, 405)
        self.assertEqual(self.client.get("/user/me/avatar").status_code, 404)

        self._logout()
        self._login("alice", "Alic3")
        self._image_bytes()
