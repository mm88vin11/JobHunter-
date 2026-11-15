"""Candidate MVP Telegram bot, split by screen.

This package is a behaviour-preserving extraction of the former single-file
``run_candidate_mvp.py`` monolith. ``run_candidate_mvp.py`` remains the
process entry point (imports, router registration, ``main()``); everything
else lives here, grouped by concern:

- ``config``     — env reading and shared module-level singletons (ADAPTER,
                    AUTOMATION, STORE, ...). Other modules must import this
                    module (``from mvp import config``) and access mutable
                    singletons as attributes (``config.ADAPTER``) rather than
                    importing the names directly, since ``main()`` reassigns
                    them at startup.
- ``states``     — FSM state groups.
- ``middleware`` — owner allowlist middleware.
- ``keyboards``  — inline keyboard builders.
- ``formatting`` — text rendering and safe-send helpers.
- ``generation`` — vacancy-to-variant generation flow.
- ``hh_sync``    — HH.ru resume/session helpers.
- ``automation`` — background auto-search / auto-apply loop.
- ``routers``    — per-screen aiogram routers, assembled in registration order.
"""
