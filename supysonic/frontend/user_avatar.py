"""Authenticated account avatar storage and routes."""

import io
import logging
import os
import secrets
import tempfile
from pathlib import Path

from flask import Response, abort, current_app, flash, jsonify, redirect, request, send_file, session, url_for
from PIL import Image, ImageOps
from werkzeug.exceptions import RequestEntityTooLarge

from ..avatar_generation import AvatarGenerationError, generate_avatar
from ..db import User
from ..managers.user import UserManager
from ..openai_recommendations import OpenAIRecommendationError, recommendation_lease
from ..profile_home import PROFILE_PERIODS
from . import frontend

logger = logging.getLogger(__name__)

AVATAR_MAX_BYTES = 4 * 1024 * 1024
AVATAR_MAX_DIMENSION = 4096
AVATAR_SIZE = (384, 384)
_CSRF_KEY = "profile_avatar_csrf"


def avatar_path(user: User) -> Path:
    base = (current_app.config["BASE"].get("tempdatafolder")
            or current_app.config["WEBAPP"]["cache_dir"])
    return (Path(base) / "account-avatars" / f"{user.id}.png").absolute()


def get_avatar_csrf_token() -> str:
    owner = str(request.user.id)
    token = session.get(_CSRF_KEY)
    if not isinstance(token, dict) or token.get("owner") != owner:
        token = {"owner": owner, "value": secrets.token_urlsafe(32)}
        session[_CSRF_KEY] = token
    return token["value"]


def _check_avatar_csrf() -> None:
    expected = session.get(_CSRF_KEY)
    supplied = request.form.get("csrf_token", "")
    if (not isinstance(expected, dict) or expected.get("owner") != str(request.user.id)
            or not secrets.compare_digest(expected["value"].encode(), supplied.encode())):
        abort(403)


def _avatar_image(data: bytes) -> Image.Image:
    if not data or len(data) > AVATAR_MAX_BYTES:
        raise ValueError("Avatar upload has an invalid size")
    with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as source:
        if max(source.size) > AVATAR_MAX_DIMENSION or getattr(source, "n_frames", 1) > 1:
            raise ValueError("Avatar must be a bounded, static image")
        with ImageOps.exif_transpose(source) as oriented:
            with oriented.convert("RGBA") as rgba:
                thumbnail = ImageOps.fit(rgba, AVATAR_SIZE, Image.Resampling.LANCZOS)
    thumbnail.info.clear()
    return thumbnail


def _save_avatar(path: Path, image: Image.Image) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            image.save(output, format="PNG")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _profile_redirect(*, reopen: bool = False):
    period = request.args.get("period")
    return redirect(url_for(
        "frontend.user_profile", uid="me",
        period=period if period in PROFILE_PERIODS else None, avatar=1 if reopen else None,
    ), code=303)


@frontend.route("/user/<uid>/avatar")
def user_avatar(uid):
    user = request.user
    if uid != "me":
        if not user.admin:
            abort(404)
        try:
            user = UserManager.get(uid)
        except (ValueError, User.DoesNotExist):
            abort(404)

    try:
        response = send_file(avatar_path(user), mimetype="image/png", conditional=False, etag=False)
    except FileNotFoundError:
        abort(404)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@frontend.route("/user/me/avatar", methods=["POST"])
def user_avatar_update():
    try:
        # Check the body size before parsing the multipart form. Allow room for
        # the file headers and CSRF field in addition to the image bytes.
        if request.content_length is None or request.content_length > AVATAR_MAX_BYTES + 65536:
            raise RequestEntityTooLarge
        _check_avatar_csrf()
        upload = request.files.get("avatar")
        data = upload.stream.read(AVATAR_MAX_BYTES + 1) if upload else b""
        image = _avatar_image(data)
    except RequestEntityTooLarge:
        flash("Choose an avatar image no larger than 4 MB.", "danger")
        return _profile_redirect(reopen=True)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        flash("Choose a static JPEG, PNG, or WebP image up to 4096 × 4096 pixels and 4 MB.", "danger")
        return _profile_redirect(reopen=True)

    try:
        with image:
            _save_avatar(avatar_path(request.user), image)
    except OSError:
        logger.exception("Failed to store account avatar")
        flash("The avatar could not be saved. Try again.", "danger")
        return _profile_redirect(reopen=True)
    else:
        flash("Avatar updated.", "success")
    return _profile_redirect()


@frontend.route("/user/me/avatar/generate", methods=["POST"])
def user_avatar_generate():
    if request.content_length is None or request.content_length > 8192:
        abort(413)
    _check_avatar_csrf()
    try:
        with recommendation_lease(f"avatar-image:{request.user.id}") as acquired:
            if not acquired:
                return jsonify(error="busy"), 409
            raw = generate_avatar(current_app.config, request.form.get("style", ""),
                                  request.form.get("description", ""))
            with _avatar_image(raw) as image, io.BytesIO() as output:
                image.save(output, format="PNG")
                return Response(output.getvalue(), mimetype="image/png", headers={
                    "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                })
    except AvatarGenerationError as exc:
        reason = str(exc)
        status = {"budget": 429, "description": 400, "disabled": 403}.get(reason, 502)
        return jsonify(error=reason), status
    except OpenAIRecommendationError:
        return jsonify(error="accounting"), 503
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return jsonify(error="response"), 502


@frontend.route("/user/me/avatar/remove", methods=["POST"])
def user_avatar_remove():
    if request.content_length is None:
        abort(403)
    if request.content_length > 65536:
        abort(413)
    _check_avatar_csrf()
    try:
        avatar_path(request.user).unlink(missing_ok=True)
    except OSError:
        logger.exception("Failed to remove account avatar")
        flash("The avatar could not be removed. Try again.", "danger")
        return _profile_redirect(reopen=True)
    else:
        flash("Default avatar restored.", "success")
    return _profile_redirect()
