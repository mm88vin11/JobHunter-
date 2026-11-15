from __future__ import annotations

import logging

import requests

log = logging.getLogger("jobhunter")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})


def get(url: str, **kw):
    kw.setdefault("timeout", 25)
    r = session.get(url, **kw)
    r.raise_for_status()
    return r


def get_json(url: str, **kw):
    import json

    return json.loads(get(url, **kw).content.decode("utf-8", "replace"))  # некоторые API шлют неверный charset
