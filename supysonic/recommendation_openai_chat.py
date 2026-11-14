"""Strict output contract for the existing outside-library artist chat."""


def _strings(count: int, length: int) -> dict:
    return {"type": "array", "maxItems": count,
            "items": {"type": "string", "maxLength": length}}


ARTIST_FIELDS = {
    "name": {"type": "string", "minLength": 1, "maxLength": 120},
    "reason": {"type": "string", "maxLength": 500},
    "genres": _strings(6, 80),
    "starterTracks": _strings(8, 120),
    "similarTo": _strings(6, 120),
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "mood": _strings(6, 80),
}
CHAT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["reply", "recommendedArtists", "nextActions"],
    "properties": {
        "reply": {"type": "string", "maxLength": 2400},
        "recommendedArtists": {
            "type": "array", "maxItems": 5,
            "items": {"type": "object", "additionalProperties": False,
                      "required": list(ARTIST_FIELDS), "properties": ARTIST_FIELDS},
        },
        "nextActions": _strings(4, 80),
    },
}
