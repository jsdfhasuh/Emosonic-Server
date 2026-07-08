from flask import current_app, flash, redirect, render_template, request, url_for

from ..mood_scene_playlist_service import (
    DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT,
    create_or_update_daily_mood_scene_playlist_for_user,
    get_daily_mood_scene_playlist_for_user,
    get_mood_scene_playlist_display_name,
    refresh_daily_mood_scene_playlists_for_user,
    save_mood_scene_playlist_copy_for_user,
)
from ..mood_scene_playlists import (
    SCENE_PLAYLISTS,
    get_mood_scene_playlist,
    list_mood_scene_playlist_keys,
)
from ..recommend import getRecommendationDay

from . import frontend


MOOD_SCENE_PAGE_TRACK_LIMIT = 10


@frontend.route("/mood-playlists")
def mood_playlists_index():
    day = getRecommendationDay()
    limit = _configured_playlist_limit()
    return render_template(
        "mood_playlists.html",
        day=day,
        cards=_build_mood_playlist_cards(request.user, day, limit),
    )


@frontend.route("/mood-playlists/refresh", methods=["POST"])
def mood_playlists_refresh_all():
    result = refresh_daily_mood_scene_playlists_for_user(
        request.user,
        limit=_configured_playlist_limit(),
        day=getRecommendationDay(),
    )
    failed = int(result.get("failed", 0) or 0)
    category = "warning" if failed else "success"
    flash(
        "Mood playlists refreshed: "
        f"{result['created']} created, {result['updated']} updated, "
        f"{result['skipped']} skipped, {failed} failed.",
        category,
    )
    return redirect(url_for("frontend.mood_playlists_index"))


@frontend.route("/mood-playlists/<scene_key>/refresh", methods=["POST"])
def mood_playlists_refresh_scene(scene_key):
    result = create_or_update_daily_mood_scene_playlist_for_user(
        request.user,
        scene_key,
        limit=_configured_playlist_limit(),
        day=getRecommendationDay(),
    )
    status = result["status"]
    if status == "created":
        flash("Mood playlist created.", "success")
    elif status == "updated":
        flash("Mood playlist updated.", "success")
    else:
        flash("No tracks available for this mood playlist yet.", "warning")
    return redirect(url_for("frontend.mood_playlists_index"))


@frontend.route("/mood-playlists/<scene_key>/save", methods=["POST"])
def mood_playlists_save_scene(scene_key):
    day = getRecommendationDay()
    source = get_daily_mood_scene_playlist_for_user(request.user, scene_key, day=day)
    if source is None:
        result = create_or_update_daily_mood_scene_playlist_for_user(
            request.user,
            scene_key,
            limit=_configured_playlist_limit(),
            day=day,
        )
        source = result.get("playlist")
    if source is None:
        flash("No tracks available to save for this mood playlist.", "warning")
        return redirect(url_for("frontend.mood_playlists_index"))

    playlist = save_mood_scene_playlist_copy_for_user(request.user, source)
    if playlist is None:
        flash("No tracks available to save for this mood playlist.", "warning")
        return redirect(url_for("frontend.mood_playlists_index"))

    flash("Mood playlist saved to your playlists.", "success")
    return redirect(url_for("frontend.playlist_details", uid=playlist.id))


def _build_mood_playlist_cards(user, day: str, limit: int) -> list:
    cards = []
    preview_limit = min(MOOD_SCENE_PAGE_TRACK_LIMIT, limit)
    for scene_key in list_mood_scene_playlist_keys():
        scene = SCENE_PLAYLISTS[scene_key]
        playlist = get_daily_mood_scene_playlist_for_user(user, scene_key, day=day)
        if playlist is not None:
            tracks = playlist.get_tracks()
            status = "ready"
            status_en = "ready"
            status_zh = "已生成"
            display_tracks = tracks[:preview_limit]
            track_count = len(tracks)
            reasons_by_track_id = {}
        else:
            preview_results = get_mood_scene_playlist(
                scene_key,
                limit=preview_limit,
                user=user,
            )
            reasons_by_track_id = {
                str(result["track"].id): result.get("reasons") or []
                for result in preview_results
            }
            status = "preview" if preview_results else "empty"
            status_en = "preview" if preview_results else "empty"
            status_zh = "预览" if preview_results else "暂无"
            display_tracks = [result["track"] for result in preview_results]
            track_count = len(display_tracks)

        cards.append(
            {
                "key": scene_key,
                "label": scene.get("label") or scene_key,
                "display_name": get_mood_scene_playlist_display_name(scene_key, day),
                "playlist": playlist,
                "status": status,
                "status_en": status_en,
                "status_zh": status_zh,
                "track_count": track_count,
                "tracks": [
                    {
                        "track": track,
                        "reason": _reason_label(
                            reasons_by_track_id.get(str(track.id), [])
                        ),
                    }
                    for track in display_tracks
                ],
            }
        )
    return cards


def _configured_playlist_limit() -> int:
    value = current_app.config["DAEMON"].get(
        "mood_scene_playlist_size",
        DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT,
    )
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MOOD_SCENE_DAILY_PLAYLIST_LIMIT
    return max(1, parsed)


def _reason_label(reasons: list) -> str:
    if reasons:
        return " · ".join(str(reason) for reason in reasons if reason)
    return "Stored in today's mood playlist."
