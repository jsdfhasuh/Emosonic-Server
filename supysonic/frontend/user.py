# This file is part of Supysonic.
# Supysonic is a Python implementation of the Subsonic server API.
#
# Copyright (C) 2013-2023 Alban 'spl0k' Féron
#
# Distributed under terms of the GNU AGPLv3 license.

import logging

from flask import current_app, flash, jsonify, redirect, render_template, request, session, url_for
from functools import wraps

from ..db import ClientPrefs, User
from ..lastfm import LastFm, is_configured
from ..listenbrainz import ListenBrainz
from ..managers.user import UserManager
from ..user_listening_profile import build_user_listening_profile

from . import admin_only, frontend

logger = logging.getLogger(__name__)


def _is_registration_enabled():
    return current_app.config["WEBAPP"].get("allow_user_registration", True)


def _get_registration_invite_code():
    return str(current_app.config["WEBAPP"].get("registration_invite_code", "") or "").strip()


def _is_registration_invite_required():
    return bool(_get_registration_invite_code())


def _is_lastfm_link_available():
    return _get_lastfm_api_key() is not None


def _get_lastfm_api_key():
    config = current_app.config["LASTFM"]
    if not is_configured(config):
        return None
    return str(config.get("api_key")).strip()


def _build_lastfm_auth_url(uid):
    callback_url = request.url_root[:-1] + url_for("frontend.lastfm_reg", uid=uid)
    return (
        "https://www.last.fm/api/auth/"
        f"?api_key={_get_lastfm_api_key()}&cb={callback_url}"
    )


def _create_registered_user(user_name, password, password_confirm, mail, registration_code=""):
    errors = []
    expected_invite_code = _get_registration_invite_code()

    if not _is_registration_enabled():
        errors.append("User registration is disabled.")

    if not user_name:
        errors.append("The name is required.")
    if not password:
        errors.append("Please provide a password.")
    elif password != password_confirm:
        errors.append("The passwords don't match.")

    if expected_invite_code:
        if not registration_code:
            errors.append("Please provide the registration invite code.")
        elif registration_code.strip() != expected_invite_code:
            errors.append("Invalid registration invite code.")

    if errors:
        return None, errors

    try:
        user = UserManager.add(user_name, password, mail=mail or "")
        return user, []
    except ValueError as exc:
        return None, [str(exc)]


def _login_registered_user(user):
    session["userid"] = str(user.id)


def _register_form_context():
    return {
        "registration_enabled": _is_registration_enabled(),
        "registration_invite_required": _is_registration_invite_required(),
        "lastfm_link_available": _is_lastfm_link_available(),
        "form_data": {
            "user": request.form.get("user", ""),
            "mail": request.form.get("mail", ""),
            "registration_code": request.form.get("registration_code", ""),
            "link_lastfm": request.form.get("link_lastfm", ""),
        },
    }


def me_or_uuid(f, arg="uid"):
    @wraps(f)
    def decorated_func(*args, **kwargs):
        if kwargs:
            uid = kwargs[arg]
        else:
            uid = args[0]

        if uid == "me":
            user = request.user
        elif not request.user.admin:
            return redirect(url_for("frontend.index"))
        else:
            try:
                user = UserManager.get(uid)
            except ValueError as e:
                flash(str(e), "danger")
                return redirect(url_for("frontend.index"))
            except User.DoesNotExist:
                flash("No such user", "danger")
                return redirect(url_for("frontend.index"))

        if kwargs:
            kwargs["user"] = user
        else:
            args = (uid, user)

        return f(*args, **kwargs)

    return decorated_func


@frontend.route("/user")
@admin_only
def user_index():
    users = list(User.select())
    summary = {
        "total": len(users),
        "admins": sum(1 for user in users if user.admin),
        "recentlyActive": sum(1 for user in users if user.last_play_date),
    }
    return render_template("users.html", users=users, summary=summary)


@frontend.route("/user/<uid>")
@me_or_uuid
def user_profile(uid, user):
    listening_profile = build_user_listening_profile(user)
    return render_template(
        "profile.html",
        user=user,
        api_key=_get_lastfm_api_key(),
        clients=user.clients,
        listening_profile=listening_profile,
    )


@frontend.route("/api/me/listening-profile")
def user_listening_profile_api():
    return jsonify(build_user_listening_profile(request.user))


@frontend.route("/user/<uid>", methods=["POST"])
@me_or_uuid
def update_clients(uid, user):
    clients_opts = {}
    for key, value in request.form.items():
        if "_" not in key:
            continue
        parts = key.split("_")
        if len(parts) != 2:
            continue
        client, opt = parts
        if not client or not opt:
            continue

        if client not in clients_opts:
            clients_opts[client] = {opt: value}
        else:
            clients_opts[client][opt] = value
    logger.debug(clients_opts)

    for client, opts in clients_opts.items():
        prefs = user.clients.where(ClientPrefs.client_name == client).first()
        if prefs is None:
            continue

        if "delete" in opts and opts["delete"] in [
            "on",
            "true",
            "checked",
            "selected",
            "1",
        ]:
            prefs.delete_instance()
            continue

        prefs.format = opts["format"] if "format" in opts and opts["format"] else None
        prefs.bitrate = (
            int(opts["bitrate"]) if "bitrate" in opts and opts["bitrate"] else None
        )
        prefs.save()

    flash("Clients preferences updated.", "success")
    return user_profile(uid, user)


@frontend.route("/user/<uid>/changeusername")
@admin_only
def change_username_form(uid):
    try:
        user = UserManager.get(uid)
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for("frontend.index"))
    except User.DoesNotExist:
        flash("No such user", "danger")
        return redirect(url_for("frontend.index"))

    return render_template("change_username.html", user=user)


@frontend.route("/user/<uid>/changeusername", methods=["POST"])
@admin_only
def change_username_post(uid):
    try:
        user = UserManager.get(uid)
    except ValueError as e:
        flash(str(e), "danger")
        return redirect(url_for("frontend.index"))
    except User.DoesNotExist:
        flash("No such user", "danger")
        return redirect(url_for("frontend.index"))

    username = request.form.get("user")
    if username in ("", None):
        flash("The username is required", "danger")
        return render_template("change_username.html", user=user)
    if user.name != username:
        try:
            User.get(name=username)
            flash("This name is already taken", "danger")
            return render_template("change_username.html", user=user)
        except User.DoesNotExist:
            pass

    if request.form.get("admin") is None:
        admin = False
    else:
        admin = True

    if user.name != username or user.admin != admin:
        user.name = username
        user.admin = admin
        user.save()
        flash(f"User '{username}' updated.", "success")
    else:
        flash(f"No changes for '{username}'.")

    return redirect(url_for("frontend.user_profile", uid=uid))


@frontend.route("/user/<uid>/changemail")
@me_or_uuid
def change_mail_form(uid, user):
    return render_template("change_mail.html", user=user)


@frontend.route("/user/<uid>/changemail", methods=["POST"])
@me_or_uuid
def change_mail_post(uid, user):
    mail = request.form.get("mail", "")
    # No validation, lol.
    user.mail = mail
    user.save()
    return redirect(url_for("frontend.user_profile", uid=uid))


@frontend.route("/user/<uid>/changepass")
@me_or_uuid
def change_password_form(uid, user):
    return render_template("change_pass.html", user=user)


@frontend.route("/user/<uid>/changepass", methods=["POST"])
@me_or_uuid
def change_password_post(uid, user):
    error = False
    if user.id == request.user.id:
        current = request.form.get("current")
        if not current:
            flash("The current password is required", "danger")
            error = True

    new, confirm = map(request.form.get, ("new", "confirm"))

    if not new:
        flash("The new password is required", "danger")
        error = True
    if new != confirm:
        flash("The new password and its confirmation don't match", "danger")
        error = True

    if not error:
        try:
            if user.id == request.user.id:
                UserManager.change_password(user.id, current, new)
            else:
                UserManager.change_password2(user.name, new)

            flash("Password changed", "success")
            return redirect(url_for("frontend.user_profile", uid=uid))
        except ValueError as e:
            flash(str(e), "danger")

    return change_password_form(uid, user)


@frontend.route("/user/add")
@admin_only
def add_user_form():
    return render_template("adduser.html")


@frontend.route("/user/add", methods=["POST"])
@admin_only
def add_user_post():
    error = False
    args = request.form.copy()
    (name, passwd, passwd_confirm) = map(
        args.pop, ("user", "passwd", "passwd_confirm"), (None,) * 3
    )
    if not name:
        flash("The name is required.", "danger")
        error = True
    if not passwd:
        flash("Please provide a password.", "danger")
        error = True
    elif passwd != passwd_confirm:
        flash("The passwords don't match.", "danger")
        error = True

    if not error:
        try:
            UserManager.add(name, passwd, **args)
            flash(f"User '{name}' successfully added", "success")
            return redirect(url_for("frontend.user_index"))
        except ValueError as e:
            flash(str(e), "danger")

    return add_user_form()


@frontend.route("/user/del/<uid>")
@admin_only
def del_user(uid):
    try:
        UserManager.delete(uid)
        flash("Deleted user", "success")
    except ValueError as e:
        flash(str(e), "danger")
    except User.DoesNotExist:
        flash("No such user", "danger")

    return redirect(url_for("frontend.user_index"))


@frontend.route("/user/<uid>/lastfm/link")
@me_or_uuid
def lastfm_reg(uid, user):
    token = request.args.get("token")
    if not token:
        flash("Missing LastFM auth token", "warning")
        return redirect(url_for("frontend.user_profile", uid=uid))

    lfm = LastFm(current_app.config["LASTFM"], user)
    status, error = lfm.link_account(token)
    if not status:
        flash(error, "danger")
    else:
        flash("Successfully linked LastFM account", "success")

    return redirect(url_for("frontend.user_profile", uid=uid))


@frontend.route("/user/<uid>/lastfm/unlink")
@me_or_uuid
def lastfm_unreg(uid, user):
    lfm = LastFm(current_app.config["LASTFM"], user)
    lfm.unlink_account()
    flash("Unlinked LastFM account", "success")
    return redirect(url_for("frontend.user_profile", uid=uid))


@frontend.route("/user/<uid>/listenbrainz/link")
@me_or_uuid
def listenbrainz_reg(uid, user):
    token = request.args.get("token")
    if not token:
        flash("Missing ListenBrainz auth token", "warning")
        return redirect(url_for("frontend.user_profile", uid=uid))

    lbz = ListenBrainz(current_app.config["LISTENBRAINZ"], user)
    status, error = lbz.link_account(token)
    if not status:
        flash(error, "danger")
    else:
        flash("Successfully linked ListenBrainz account", "success")

    return redirect(url_for("frontend.user_profile", uid=uid))


@frontend.route("/user/<uid>/listenbrainz/unlink")
@me_or_uuid
def listenbrainz_unreg(uid, user):
    lbz = ListenBrainz(current_app.config["LISTENBRAINZ"], user)
    lbz.unlink_account()
    flash("Unlinked ListenBrainz account", "success")
    return redirect(url_for("frontend.user_profile", uid=uid))


@frontend.route("/user/login", methods=["GET", "POST"])
def login():
    return_url = request.args.get("returnUrl") or request.form.get("returnUrl") or url_for("frontend.index")
    if request.user:
        flash("Already logged in")
        return redirect(return_url)

    if request.method == "GET":
        return render_template("login.html")

    name, password = map(request.form.get, ("user", "password"))
    error = False
    if not name:
        flash("Missing user name", "danger")
        error = True
    if not password:
        flash("Missing password", "danger")
        error = True

    if not error:
        user = UserManager.try_auth(name, password)
        if user:
            logger.info("Logged user %s (IP: %s)", name, request.remote_addr)
            session["userid"] = str(user.id)
            flash("Logged in!", "success")
            return redirect(return_url)
        else:
            logger.error(
                "Failed login attempt for user %s (IP: %s)", name, request.remote_addr
            )
            flash("Wrong username or password", "danger")

    return render_template("login.html")


@frontend.route("/user/register", methods=["GET", "POST"])
def register():
    if request.user:
        flash("Already logged in")
        return redirect(url_for("frontend.index"))

    if not _is_registration_enabled():
        flash("User registration is disabled.", "warning")
        return redirect(url_for("frontend.login"))

    return_url = request.args.get("returnUrl") or request.form.get("returnUrl") or url_for("frontend.index")
    if request.method == "GET":
        return render_template("register.html", return_url=return_url, **_register_form_context())

    user_name = request.form.get("user", "")
    password = request.form.get("passwd", "")
    password_confirm = request.form.get("passwd_confirm", "")
    mail = request.form.get("mail", "")
    registration_code = request.form.get("registration_code", "")
    user, errors = _create_registered_user(
        user_name,
        password,
        password_confirm,
        mail,
        registration_code,
    )
    if errors:
        for error in errors:
            flash(error, "danger")
        return render_template("register.html", return_url=return_url, **_register_form_context())

    _login_registered_user(user)
    flash("Account created and logged in!", "success")
    if request.form.get("link_lastfm") and _is_lastfm_link_available():
        return redirect(_build_lastfm_auth_url("me"))
    return redirect(return_url)


@frontend.route("/user/register.json", methods=["POST"])
def register_json():
    if request.user:
        return jsonify({"ok": False, "error": "Already logged in."}), 400
    if not _is_registration_enabled():
        return jsonify({"ok": False, "error": "User registration is disabled."}), 403

    data = request.get_json(silent=True)
    if data is None:
        data = request.form

    user_name = data.get("user", "")
    password = data.get("password", "")
    password_confirm = data.get("passwordConfirm", "")
    mail = data.get("mail", "")
    registration_code = data.get("registrationCode", "")
    user, errors = _create_registered_user(
        user_name,
        password,
        password_confirm,
        mail,
        registration_code,
    )
    if errors:
        return jsonify({"ok": False, "error": errors[0]}), 400

    _login_registered_user(user)
    return jsonify(
        {
            "ok": True,
            "user": {
                "id": str(user.id),
                "name": user.name,
            },
        }
    )


@frontend.route("/user/logout")
def logout():
    session.clear()
    flash("Logged out!", "success")
    return redirect(url_for("frontend.login"))
