import hmac
import secrets
from uuid import UUID

from flask import abort, current_app, flash, redirect, render_template, request, session, url_for

from ..album_discovery_daily import album_discovery_metrics
from ..album_discovery_service import AlbumDiscoveryError, AlbumDiscoveryService
from ..album_discovery_settings import AlbumDiscoverySettingsError, album_discovery_target_user
from . import frontend


_OUTCOMES = {
    "ready": ("专辑已收齐。", "success"),
    "submitted": ("已提交国区整专下载。", "success"),
    "associated": ("已关联既有 Oracle 任务。", "success"),
    "unknown": ("Oracle 受理状态待核对，已保留原订单。", "warning"),
    "pending": ("已保留原订单，等待核对。", "info"),
    "rejected": ("Oracle 已明确拒绝该订单。", "warning"),
    "limit_reached": ("今日新派单名额已用完或暂停。", "warning"),
    "wait_oracle": ("等待核实 Oracle 在途任务和交付。", "warning"),
    "submission_disabled": ("自动派单已关闭。", "warning"),
}


def _check_account() -> None:
    if request.user is None or request.user.name != album_discovery_target_user(current_app.config):
        abort(404)


def _csrf_token() -> str:
    token = session.get("album_discovery_csrf")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["album_discovery_csrf"] = token
    return token


def _check_csrf() -> None:
    expected = session.get("album_discovery_csrf")
    supplied = request.form.get("csrf", "")
    if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
        abort(403)


@frontend.route("/recommendations/discovery", methods=["GET", "POST"])
def album_discovery_index():
    _check_account()
    generate = request.method == "POST"
    if generate:
        _check_csrf()
    preview = metrics = error = None
    tracked = ()
    try:
        service = AlbumDiscoveryService.from_config(current_app.config, with_catalog=generate)
        preview = service.preview(request.user, generate=generate)
        metrics = album_discovery_metrics(request.user, service.library_key)
        tracked = service.tracked_releases(request.user)
    except (AlbumDiscoveryError, AlbumDiscoverySettingsError) as exc:
        error = str(exc)
    response = current_app.make_response(render_template(
        "album_discovery.html", preview=preview, tracked=tracked, metrics=metrics, error=error,
        csrf_token=_csrf_token(),
    ))
    response.headers["Cache-Control"] = "no-store"
    return response


@frontend.route("/recommendations/discovery/<uuid:preview_id>/confirm", methods=["POST"])
def album_discovery_confirm(preview_id: UUID):
    _check_account()
    _check_csrf()
    selected = request.form.getlist("album_id")
    if len(selected) != 1:
        abort(400)
    try:
        result = AlbumDiscoveryService.from_config(current_app.config).confirm(
            request.user, preview_id, selected[0],
            representative_song_id=request.form.get("representative_song_id"),
        )
        message, severity = _OUTCOMES[result.status]
        flash(message, severity)
    except (AlbumDiscoveryError, AlbumDiscoverySettingsError) as exc:
        flash(str(exc), "warning")
    return redirect(url_for("frontend.album_discovery_index"), code=303)
