"""Apply one listening-inspired portrait per Shanghai calendar day."""

from __future__ import annotations

import logging
from datetime import datetime

from .account_avatars import account_avatar_path, avatar_png, publish_avatar, write_atomic
from .avatar_generation import AvatarGenerationError, STYLES, generate_avatar, generation_enabled
from .db import User
from .metadata_budget import SHANGHAI
from .openai_recommendations import recommendation_settings
from .profile_home import build_profile_home

logger = logging.getLogger(__name__)


def rotate_daily_avatar(config, at: datetime | None = None) -> str:
    settings = recommendation_settings(config)
    username = settings.get("daily_avatar_username")
    today = (at or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if not username or not generation_enabled(config) or today.hour < 8:
        return "idle"
    user = User.get(User.name == username)
    current = account_avatar_path(config, user)
    folder = current.parent / "daily" / current.stem
    folder.mkdir(parents=True, exist_ok=True)
    day = today.date()
    attempt = folder / f"{day}.attempt"
    pending = folder / f"{day}.pending.png"
    if not pending.is_file():
        if attempt.exists():
            return "idle"
        home = build_profile_home(user, "7")
        labels = [item["value"] for item in home["moods"][:3] + home["tags"][:3]]
        subject = str(settings.get("daily_avatar_subject") or "a music listener")[:80]
        theme = f"{subject}. Listening mood and sounds: {', '.join(labels)}" if labels else subject
        style = tuple(STYLES)[day.toordinal() % len(STYLES)]
        try:
            # Exclusive claim survives daemon restarts and ambiguous upstream timeouts.
            attempt.touch(exist_ok=False)
        except FileExistsError:
            return "idle"
        try:
            raw = generate_avatar(config, style, theme[:160])
        except AvatarGenerationError as exc:
            # Budget admission failed before any billable call. Check again later.
            if str(exc) == "budget":
                attempt.unlink(missing_ok=True)
            raise
        write_atomic(pending, avatar_png(raw))
    publish_avatar(current, pending=pending)
    logger.info("daily_avatar_applied day=%s account=%s", day, user.id)
    return "applied"
