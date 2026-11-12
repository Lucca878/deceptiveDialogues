"""Private eligible records for tests that deliberately seed synthetic progress."""

from datetime import datetime, timezone
from secrets import token_hex

import server


def retain_eligible_submission(client):
    # Avoid 20,000 extra HTTP screenings in the full-capacity matching tests.
    # Real validation and retention are exercised through HTTP in screening tests.
    submission_id = token_hex(32)
    with server.rooms_lock:
        server.screening_submissions[submission_id] = {
            "version": server.SCREENING_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "answers": {
                city_id: {"visited": "yes" if index < 5 else "no", "familiarity": 4}
                for index, (city_id, _, _) in enumerate(server.CANDIDATE_CITIES)
            },
        }
    with client.session_transaction() as browser_session:
        browser_session["screening_submission_id"] = submission_id
    return submission_id