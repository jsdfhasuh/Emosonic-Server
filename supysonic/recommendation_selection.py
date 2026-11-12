"""Deterministic artist diversity and rotation for ranked recommendations."""

import math
from collections import Counter
from typing import Iterable, List, Optional, TypeVar


DEFAULT_MAX_ARTIST_RATIO = 0.10
_Track = TypeVar("_Track")


def select_diverse_recommendations(
    ranked_tracks: Iterable[_Track],
    num_songs: int,
    recent_track_ids: Optional[Iterable[object]] = None,
    minimum_fresh_tracks: int = 0,
    max_artist_ratio: float = DEFAULT_MAX_ARTIST_RATIO,
) -> List[_Track]:
    """Select tracks under an artist cap, reserving fresh slots when feasible.

    Only an insufficient artist pool relaxes the cap. An infeasible fresh-track
    target does not. The selected tracks retain their input ranking.
    """
    if num_songs <= 0:
        return []

    candidates = []
    seen_ids = set()
    for track in ranked_tracks:
        track_id = str(getattr(track, "id"))
        if track_id in seen_ids:
            continue
        seen_ids.add(track_id)
        artist_id = str(getattr(track, "artist_id", None) or "")
        candidates.append((track, track_id, artist_id))
    if not candidates:
        return []

    try:
        ratio = float(max_artist_ratio)
    except (TypeError, ValueError, OverflowError):
        ratio = DEFAULT_MAX_ARTIST_RATIO
    if not math.isfinite(ratio) or not 0 < ratio <= 1:
        ratio = DEFAULT_MAX_ARTIST_RATIO

    target_count = min(num_songs, len(candidates))
    artist_pool = Counter(artist_id for _, _, artist_id in candidates)
    artist_cap = max(1, math.ceil(num_songs * ratio))

    # Find the smallest cap that can fill the requested selection.
    capacity = sum(min(count, artist_cap) for count in artist_pool.values())
    if capacity < target_count:
        low, high = artist_cap + 1, target_count
        while low < high:
            middle = (low + high) // 2
            capacity = sum(min(count, middle) for count in artist_pool.values())
            if capacity >= target_count:
                high = middle
            else:
                low = middle + 1
        artist_cap = low

    recent_ids = {str(track_id) for track_id in recent_track_ids or ()}
    fresh_target = min(target_count, max(0, minimum_fresh_tracks))
    selected_ids = set()
    selected_artists = Counter()

    for _, track_id, artist_id in candidates:
        if len(selected_ids) >= fresh_target:
            break
        if track_id in recent_ids or selected_artists[artist_id] >= artist_cap:
            continue
        selected_ids.add(track_id)
        selected_artists[artist_id] += 1

    for _, track_id, artist_id in candidates:
        if len(selected_ids) >= target_count:
            break
        if track_id in selected_ids or selected_artists[artist_id] >= artist_cap:
            continue
        selected_ids.add(track_id)
        selected_artists[artist_id] += 1

    return [track for track, track_id, _ in candidates if track_id in selected_ids]
