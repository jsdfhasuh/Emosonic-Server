# This file is part of Supysonic / Emosonic Server.
# Distributed under terms of the GNU AGPLv3 license.

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from peewee import fn

from .. import DOWNLOAD_URL, VERSION
from ..db import Album, Artist, Track, User
from ..managers.user import UserManager

player = Blueprint("player", __name__)
WEB_PLAYER_CLIENT_NAME = "emosonic-web-player"
DEFAULT_TRACK_LIMIT = 100
MAX_TRACK_LIMIT = 500


@player.context_processor
def inject_metadata():
    return {
        "version": VERSION,
        "download_url": DOWNLOAD_URL,
        "allow_user_registration": current_app.config["WEBAPP"].get(
            "allow_user_registration", True
        ),
    }


@player.before_request
def login_check():
    request.user = None
    if session.get("userid"):
        try:
            request.user = UserManager.get(session.get("userid"))
            return
        except (ValueError, User.DoesNotExist):
            session.clear()

    flash("Please login")
    return redirect(
        url_for(
            "frontend.login",
            returnUrl=request.script_root + request.url[len(request.url_root) - 1 :],
        )
    )


def _duration_text(total_seconds):
    try:
        total_seconds = int(total_seconds or 0)
    except (TypeError, ValueError):
        total_seconds = 0
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _track_payload(track):
    artist_name = track.artist.get_artist_name() if track.artist else ""
    album_name = track.album.name if track.album else ""
    return {
        "id": str(track.id),
        "title": track.title,
        "artist": artist_name,
        "album": album_name,
        "duration": track.duration or 0,
        "durationText": _duration_text(track.duration),
        "playCount": track.play_count or 0,
        "streamUrl": url_for(
            "api.stream_media",
            id=str(track.id),
            c=WEB_PLAYER_CLIENT_NAME,
        ),
        "coverUrl": url_for(
            "frontend.control_cover",
            eid=str(track.id),
            size=320,
        ),
    }


@player.route("/player")
def player_index():
    return render_template("player.html")


@player.route("/player/tracks")
def player_tracks():
    query_text = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit") or DEFAULT_TRACK_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_TRACK_LIMIT
    limit = max(1, min(limit, MAX_TRACK_LIMIT))

    query = Track.select(Track, Album, Artist).join(Album).switch(Track).join(Artist)
    if query_text:
        keyword = query_text.lower()
        query = query.where(
            fn.LOWER(Track.title).contains(keyword)
            | fn.LOWER(Artist.name).contains(keyword)
            | fn.LOWER(Album.name).contains(keyword)
        )

    tracks = (
        query.order_by(Artist.name, Album.name, Track.disc, Track.number, Track.title)
        .limit(limit)
    )

    return jsonify(
        {
            "tracks": [_track_payload(track) for track in tracks],
            "query": query_text,
            "limit": limit,
            "client": WEB_PLAYER_CLIENT_NAME,
        }
    )
