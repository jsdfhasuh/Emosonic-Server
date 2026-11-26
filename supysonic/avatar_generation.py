"""On-demand, budgeted image generation for the web avatar editor."""

import base64
import json
import logging
from types import SimpleNamespace

import requests

from .llm_client import LlmConfigError
from .metadata_budget import BudgetExceeded, BudgetUnavailable, MetadataBudget
from .openai_recommendations import recommendation_settings
from .track_metadata_config import get_track_metadata_llm_config


logger = logging.getLogger(__name__)
MODEL = "gpt-image-2.6-flare"
ENDPOINT = "https://api.openai.com/v1"
STYLES = {
    "print": "a vintage screen-printed record sleeve with bold shapes",
    "neon": "a neon illustration with a dark indigo background",
    "watercolor": "a soft watercolor illustration on textured paper",
}


class AvatarGenerationError(Exception):
    """The reason is safe to translate and display to the account holder."""


def generation_enabled(config) -> bool:
    settings = config["RECOMMENDATION_AGENT"]
    return (settings.get("enabled") is True and settings.get("provider") == "openai"
            and settings.get("avatar_generation_enabled") is True
            and str(settings.get("api_base_url")).rstrip("/") == ENDPOINT)


def generate_avatar(config, style: str, description: str) -> bytes:
    if not generation_enabled(config):
        raise AvatarGenerationError("disabled")
    if style not in STYLES or not description.strip() or len(description) > 160:
        raise AvatarGenerationError("description")
    prompt = (
        f"Design a square avatar inspired by music, in the style of {STYLES[style]}. "
        "One clear central motif, legible at 56px and within a circular crop. "
        f"No lettering or logos. The visual theme is: {description.strip()}"
    )
    settings = recommendation_settings(config)
    try:
        credentials = get_track_metadata_llm_config(SimpleNamespace(TRACK_METADATA_LLM=settings))
        budget = MetadataBudget(settings["_budget_config"])
        # Fixed square, single medium-quality output. Reserve a conservative
        # image-token allowance and settle the returned text/image usage.
        allowance = 30 * 4096 + 5 * (len(prompt.encode("utf-8")) + 256)
        reservation = budget.reserve("avatar_image", allowance)
        with requests.post(
            ENDPOINT + "/images/generations",
            headers={"Authorization": "Bearer " + credentials["api_key"]},
            json={"model": MODEL, "prompt": prompt, "n": 1, "size": "1024x1024",
                  "quality": "medium", "complexity": "low", "output_format": "jpeg"},
            timeout=(8, 120), allow_redirects=False, stream=True,
        ) as response:
            if response.status_code != 200:
                logger.warning("avatar_generation_rejected status=%s", response.status_code)
                raise AvatarGenerationError("upstream")
            content = bytearray()
            for chunk in response.iter_content(65536):
                if len(content) + len(chunk) > 6 * 1024 * 1024:
                    raise AvatarGenerationError("response")
                content.extend(chunk)
        result = json.loads(content)
        usage = result["usage"]
        inputs, outputs, total = (usage[key] for key in ("input_tokens", "output_tokens", "total_tokens"))
        if (any(type(n) is not int or n < 0 for n in (inputs, outputs, total))
                or total != inputs + outputs or outputs == 0):
            raise AvatarGenerationError("accounting")
        # There is no reference image in this request. If the provider does
        # report image input separately, account for it at its own rate.
        image_inputs = usage.get("input_tokens_details", {}).get("image_tokens", 0)
        if type(image_inputs) is not int or not 0 <= image_inputs <= inputs:
            raise AvatarGenerationError("accounting")
        cost = (inputs - image_inputs) * 5 + image_inputs * 8 + outputs * 30
        budget.settle(reservation, cost)
        logger.info("avatar_generation_accounted model=%s cost_micro_usd=%s", MODEL, cost)
        if cost > allowance:
            raise AvatarGenerationError("accounting")
        return base64.b64decode(result["data"][0]["b64_json"], validate=True)
    except BudgetExceeded:
        raise AvatarGenerationError("budget") from None
    except (BudgetUnavailable, LlmConfigError):
        raise AvatarGenerationError("accounting") from None
    except requests.RequestException:
        raise AvatarGenerationError("network") from None
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise AvatarGenerationError("response") from None
