"""Load the ordered city questionnaire data from its editable JSON source."""

import json
from pathlib import Path
import re


# Keep the metadata source path relative to the repository, independent of the current working directory.
CANDIDATE_CITIES_SOURCE = "config/city_candidates.json"
CANDIDATE_CITIES_PATH = Path(__file__).resolve().parent / CANDIDATE_CITIES_SOURCE


def load_candidate_cities(path: Path = CANDIDATE_CITIES_PATH) -> tuple[tuple[str, str, str], ...]:
    """Read and validate ordered city IDs, display names, and country labels."""
    try:
        candidates = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not load candidate cities from {path}: {error}") from error

    # Reject malformed city records before their values reach HTML names or export columns.
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Candidate cities must be a non-empty JSON array.")

    loaded = []
    seen_ids = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict) or set(candidate) != {"id", "name", "country"}:
            raise ValueError(f"Candidate city {index + 1} must contain only id, name, and country.")
        city_id, name, country = candidate["id"], candidate["name"], candidate["country"]
        if (
            not isinstance(city_id, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", city_id)
            or city_id in seen_ids
        ):
            raise ValueError(f"Candidate city {index + 1} must have a unique lowercase slug ID.")
        if any(not isinstance(value, str) or not value.strip() for value in (name, country)):
            raise ValueError(f"Candidate city {index + 1} must have a non-empty name and country.")
        seen_ids.add(city_id)
        loaded.append((city_id, name.strip(), country.strip()))

    # A tuple preserves the JSON order while preventing callers from changing the shared candidate set.
    return tuple(loaded)


# Server screening and raw exports use this same validated list in the order participants see it.
CANDIDATE_CITIES = load_candidate_cities()