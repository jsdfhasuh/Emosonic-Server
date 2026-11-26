import base64
import io
import json
from unittest.mock import MagicMock, patch

from PIL import Image

from supysonic.avatar_generation import ENDPOINT, MODEL
from supysonic.db import Meta, User

from .frontendtestbase import FrontendTestBase


class AvatarGenerationTestCase(FrontendTestBase):
    def setUp(self):
        super().setUp()
        cfg = self.client.application.config
        cfg["RECOMMENDATION_AGENT"] = dict(
            cfg["RECOMMENDATION_AGENT"], enabled=True, provider="openai", api_key="test-key",
            model="gpt-6-luna", api_base_url=ENDPOINT, avatar_generation_enabled=True,
        )
        cfg["TRACK_METADATA_LLM"] = {"monthly_budget_usd": 10, "daily_budget_usd": 2}
        cfg["BASE"] = dict(cfg["BASE"], tempdatafolder=cfg["WEBAPP"]["cache_dir"])
        self._login("bob", "B0b")
        self.client.get("/user/me")
        with self.client.session_transaction() as session:
            self.token = session["profile_avatar_csrf"]["value"]
        self.form = {"csrf_token": self.token, "style": "print", "description": "rainy piano"}
        self.path = "/user/me/avatar/generate"
        mock_post = patch("supysonic.avatar_generation.requests.post")
        self.post = mock_post.start()
        self.addCleanup(mock_post.stop)

    def upstream(self, usage, payload=None):
        png = io.BytesIO()
        with Image.new("RGB", (96, 96), "teal") as image:
            image.save(png, format="PNG")
        content = json.dumps({"usage": usage, "data": [{"b64_json": (
            base64.b64encode(png.getvalue()).decode() if payload is None else payload
        )}]}).encode()
        response = MagicMock(status_code=200)
        response.iter_content.return_value = [content]
        self.post.return_value.__enter__.return_value = response

    def test_generate_then_explicitly_use(self):
        self.assertIn("data-avatar-generate", self.client.get("/user/me").data)
        self.post.assert_not_called()
        self.upstream({"input_tokens": 40, "output_tokens": 823, "total_tokens": 863})
        with self.client.open(self.path, method="POST", data=self.form) as preview:
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.mimetype, "image/png")
            data = preview.get_data()
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.size, (384, 384))
        self.assertEqual(self.client.get("/user/me/avatar").status_code, 404)
        args, kwargs = self.post.call_args
        self.assertEqual(args, (ENDPOINT + "/images/generations",))
        self.assertEqual(kwargs["json"]["model"], MODEL)
        self.assertIn("rainy piano", kwargs["json"]["prompt"])
        self.assertNotIn("bob", kwargs["json"]["prompt"])
        self.assertFalse(kwargs["allow_redirects"])
        charged = Meta.select().where(Meta.key.startswith("mb:r:")).get()
        self.assertEqual(json.loads(charged.value)["a"], 40 * 5 + 823 * 30)
        result = self.client.post("/user/me/avatar?period=30", data={
            "csrf_token": self.token, "avatar": (io.BytesIO(data), "avatar.png"),
        })
        self.assertEqual(result.location, "/user/me?period=30")
        with self.client.open("/user/me/avatar") as saved:
            self.assertEqual(saved.status_code, 200)

    def test_owner_scope_csrf_and_budget(self):
        self.assertEqual(self.client.post(self.path, data=dict(self.form, csrf_token="wrong")).status_code, 403)
        self.assertEqual(self.client.post(self.path, data=dict(self.form, style="bad")).status_code, 400)
        self.client.application.config["TRACK_METADATA_LLM"]["daily_budget_usd"] = 0.01
        self.assertEqual(self.client.post(self.path, data=self.form).json["error"], "budget")
        self.client.application.config["RECOMMENDATION_AGENT"]["avatar_generation_enabled"] = False
        self.assertNotIn("data-avatar-generate", self.client.get("/user/me").data)
        self.assertEqual(self.client.post(self.path, data=self.form).status_code, 403)
        self._logout()
        self.assertEqual(self.client.post(self.path, data=self.form).status_code, 302)
        self._login("alice", "Alic3")
        self.client.application.config["RECOMMENDATION_AGENT"]["avatar_generation_enabled"] = True
        bob = User.get(User.name == "bob")
        self.assertNotIn("data-avatar-generate", self.client.get(f"/user/{bob.id}").data)
        self.post.assert_not_called()

    def test_unverifiable_usage_keeps_reservation_and_current_avatar(self):
        self.upstream({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        response = self.client.post(self.path, data=self.form)
        self.assertEqual(response.json["error"], "accounting")
        self.assertIsNone(json.loads(Meta.select().where(Meta.key.startswith("mb:r:")).get().value)["a"])
        self.assertEqual(self.client.get("/user/me/avatar").status_code, 404)
