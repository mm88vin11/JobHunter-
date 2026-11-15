"""Environment reading and shared module-level singletons.

``ADAPTER``, ``FALLBACK_ADAPTER`` and ``MODEL_DEGRADED_UNTIL`` are reassigned
at runtime (``main()`` sets the adapters at startup; ``_generate_with_failover``
flips ``MODEL_DEGRADED_UNTIL`` on provider failure). Because a plain
``from mvp.config import ADAPTER`` binds a local name to whatever object
``ADAPTER`` pointed to *at import time*, consumers must instead do
``from mvp import config`` and read/write ``config.ADAPTER`` etc., so every
module observes the same, current value. ``STORE``, ``AUTOMATION``,
``AUTOMATION_LOCK``, ``HH_BROWSER_SESSIONS`` and ``ROOT`` are never rebound
(only mutated in place via their own methods / dict operations), so importing
those names directly is safe.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from candidate.automation import AutomationStore
from candidate.service import CandidateAdapter
from candidate.storage import VariantStore
from hh.api_client import HHApiClient
from hh.auth import HHAuth

# mvp/config.py lives one directory below the project root (where
# run_candidate_mvp.py itself sits), so this needs one extra `.parent`
# compared to the original `Path(__file__).resolve().parent` in the
# single-file version.
ROOT = Path(__file__).resolve().parent.parent

STORE = VariantStore(os.environ.get("LOCAL_VARIANT_DB_PATH", ROOT / "var" / "candidate_mvp.sqlite3"))
AUTOMATION = AutomationStore(os.environ.get("LOCAL_VARIANT_DB_PATH", ROOT / "var" / "candidate_mvp.sqlite3"))
ADAPTER: CandidateAdapter | None = None
FALLBACK_ADAPTER: CandidateAdapter | None = None
AUTOMATION_LOCK = asyncio.Lock()
MODEL_DEGRADED_UNTIL = 0.0
HH_BROWSER_SESSIONS: dict[int, tuple[HHAuth, HHApiClient]] = {}

# CRM settings defaults -- mvp/settings_store.py owns validation/read/write
# for these (get_crm_*/set_crm_* accessors); the constants live here rather
# than in settings_store.py because this task's brief calls for defaults to
# live alongside config.py's other module-level defaults. No env-var
# overrides: this file only does os.environ overrides for path-like values
# (LOCAL_VARIANT_DB_PATH above), and none of these four settings are paths.
CRM_SILENCE_DAYS_DEFAULT = 3
CRM_SILENCE_DAYS_MIN, CRM_SILENCE_DAYS_MAX = 1, 30

CRM_DIGEST_ENABLED_DEFAULT = True
CRM_DIGEST_TIME_DEFAULT = "09:00"
CRM_DIGEST_TIMEZONE = os.environ.get("BOT_TIMEZONE", "UTC")

CRM_ARCHIVE_AFTER_DAYS_DEFAULT = 30
CRM_ARCHIVE_AFTER_DAYS_MIN, CRM_ARCHIVE_AFTER_DAYS_MAX = 7, 365

#: Age past which a thread leaves the CRM no matter whose move it was.
#: `crm_archive_after_days` above only ever touches threads where the employer
#: owes the next step -- anything the owner still has to answer, and every
#: rejection, is deliberately kept on screen. That protection is right for a
#: thread from last week and wrong for one from spring: a reply owed since
#: March is not a task, and a rejection from March is not news. Both were
#: sitting in the working screen forever because each guard held on its own.
#: Not a per-user setting: there is no screen to change it from, and the two
#: knobs that do have one already cover the tuning worth doing.
CRM_STALE_AFTER_DAYS = 60


def webapp_public_url() -> str:
    """Origin the Mini App is served from, or "" when it is not configured.

    Read on every call instead of cached at import, because the address can
    change between restarts when a tunnel provides it. Anything that is not
    `https://` is treated as absent: Telegram refuses to open other schemes,
    and a button pointing at one fails at press time with a confusing error
    rather than simply not being there.
    """
    url = os.environ.get("WEBAPP_PUBLIC_URL", "").strip()
    return url if url.startswith("https://") else ""

DEMO_VACANCY = {
    "id": "demo-vacancy",
    "name": "Демонстрационная вакансия",
    "employer_name": "Вымышленная компания",
    "source": "demo",
    "source_url": "",
    "description": (
        "Это полностью вымышленный пример для проверки интерфейса. "
        "Описание не связано с реальной компанией, вакансией или кандидатом."
    ),
}
