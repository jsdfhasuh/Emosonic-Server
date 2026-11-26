"""Authenticated account avatar storage and routes."""

import logging
import secrets
from pathlib import Path

from flask import Response, abort, current_app, flash, jsonify, redirect, request, session, url_for
from PIL import Image
from werkzeug.exceptions import RequestEntityTooLarge

from ..account_avatars import AVATAR_MAX_BYTES, account_avatar_path, avatar_png, publish_avatar, send_account_avatar
from ..avatar_generation import AvatarGenerationError, generate_avatar, generation_enabled
from ..db import User
from ..managers.user import UserManager
from ..openai_recommendations import OpenAIRecommendationError, recommendation_lease
from ..profile_home import PROFILE_PERIODS
from . import frontend

logger = logging.getLogger(__name__)

_CSRF_KEY = "profile_avatar_csrf"


def avatar_path(user: User) -> Path:
    return account_avatar_path(current_app.config, user)


@frontend.app_template_global()
def account_avatar_info(user: User) -> dict:
    config = current_app.config
    return {
        "src": url_for("frontend.user_avatar", uid="me" if user.id == request.user.id else user.id),
        "initial": user.name[:1].upper(),
        "exists": avatar_path(user).is_file(),
        "daily": config["RECOMMENDATION_AGENT"].get("daily_avatar_username") == user.name
                 and generation_enabled(config),
    }


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
        return send_account_avatar(user)
    except FileNotFoundError:
        abort(404)


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
        png = avatar_png(data)
    except RequestEntityTooLarge:
        flash("Choose an avatar image no larger than 4 MB.", "danger")
        return _profile_redirect(reopen=True)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        flash("Choose a static JPEG, PNG, or WebP image up to 4096 × 4096 pixels and 4 MB.", "danger")
        return _profile_redirect(reopen=True)

    try:
        publish_avatar(avatar_path(request.user), data=png)
    except (OSError, OpenAIRecommendationError):
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
            return Response(avatar_png(raw), mimetype="image/png", headers={
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
        publish_avatar(avatar_path(request.user))
    except (OSError, OpenAIRecommendationError):
        logger.exception("Failed to remove account avatar")
        flash("The avatar could not be removed. Try again.", "danger")
        return _profile_redirect(reopen=True)
    else:
        flash("Default avatar restored.", "success")
    return _profile_redirect()
