# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2013-2022 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

from flask import current_app, request
from functools import wraps

from ..account_avatars import send_account_avatar
from ..db import User
from ..managers.user import UserManager
from ..profile_home import PROFILE_PERIODS, build_profile_home
from ..recommendation_openai_profile import get_cached_openai_profile, openai_profile_enabled

from . import decode_password, api_routing
from .exceptions import Forbidden, NotFound, UnsupportedParameter


def admin_only(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not request.user.admin:
            raise Forbidden()
        return f(*args, **kwargs)

    return decorated


@api_routing("/getUser")
def user_info():
    username = request.values["username"]

    if username != request.user.name and not request.user.admin:
        raise Forbidden()

    user = User.get(name=username)
    return request.formatter("user", user.as_subsonic_user())


@api_routing("/getUsers")
@admin_only
def users_info():
    return request.formatter(
        "users", {"user": [u.as_subsonic_user() for u in User.select()]}
    )


@api_routing("/getAvatar")
def user_avatar():
    username = request.values.get("username", request.user.name)
    user = request.user
    if username != user.name:
        if not user.admin:
            raise Forbidden()
        user = User.get_or_none(User.name == username)
        if user is None:
            raise NotFound("User")
    try:
        return send_account_avatar(user)
    except FileNotFoundError:
        raise NotFound("Avatar") from None


@api_routing("/getEmoListeningProfile")
def listening_profile():
    period = request.values.get("period", "7")
    if period not in PROFILE_PERIODS:
        raise UnsupportedParameter("period")
    home = build_profile_home(request.user, period)
    profile = {
        "period": period, "playCount": home["play_count"], "trackCount": home["track_count"],
        "mood": [item["value"] for item in home["moods"]],
        "tag": [item["value"] for item in home["tags"]],
        "song": [{"title": row["title"], "artist": row["artist"], "playCount": row["plays"]}
                 for row in home["top_tracks"]],
    }
    if openai_profile_enabled(current_app.config):
        analysis = get_cached_openai_profile(request.user)
        if analysis:
            profile["summary"] = analysis["summary"]
    response = request.formatter("listeningProfile", profile)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def get_roles_dict():
    roles = {}
    for role in ("admin", "jukebox"):
        value = request.values.get(role + "Role")
        value = value in (True, "True", "true", 1, "1")
        roles[role] = value

    return roles


@api_routing("/createUser")
@admin_only
def user_add():
    username = request.values["username"]
    password = request.values["password"]
    email = request.values["email"]
    roles = get_roles_dict()

    password = decode_password(password)
    UserManager.add(username, password, mail=email, **roles)

    return request.formatter.empty


@api_routing("/deleteUser")
@admin_only
def user_del():
    username = request.values["username"]
    UserManager.delete_by_name(username)

    return request.formatter.empty


@api_routing("/changePassword")
def user_changepass():
    username = request.values["username"]
    password = request.values["password"]

    if username != request.user.name and not request.user.admin:
        raise Forbidden()

    password = decode_password(password)
    UserManager.change_password2(username, password)

    return request.formatter.empty


@api_routing("/updateUser")
@admin_only
def user_edit():
    username = request.values["username"]
    user = User.get(name=username)

    if "password" in request.values:
        password = decode_password(request.values["password"])
        UserManager.change_password2(user, password)

    email, admin, jukebox = map(
        request.values.get, ("email", "adminRole", "jukeboxRole")
    )
    if email is not None:
        user.mail = email

    if admin is not None:
        admin = admin in (True, "True", "true", 1, "1")
        user.admin = admin

    if jukebox is not None:
        jukebox = jukebox in (True, "True", "true", 1, "1")
        user.jukebox = jukebox

    user.save()

    return request.formatter.empty
