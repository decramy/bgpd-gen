"""Generieke NetBox REST-client (GET/POST/PATCH + gefilterd zoeken) plus
token-inlezen. Domeinspecifieke lookups (router/scope/device/sessions) staan
in lib/router.py en lib/sessions.py, niet hier."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from lib.config import NETBOX_BASE, TOKEN_FILE
from lib.errors import BgpdGenError


def load_token() -> str:
    if not TOKEN_FILE.exists():
        raise BgpdGenError(f"{TOKEN_FILE} ontbreekt")
    token = TOKEN_FILE.read_text().strip()
    if not token:
        raise BgpdGenError(f"{TOKEN_FILE} is leeg")
    return token


def _netbox_request(path: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    url = f"{NETBOX_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Token {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise BgpdGenError(f"NetBox {method} {path} gaf HTTP {e.code}: {detail[:500]}") from e
    except urllib.error.URLError as e:
        raise BgpdGenError(f"NetBox {method} {path} onbereikbaar: {e.reason}") from e


def netbox_get(path: str, token: str) -> dict:
    return _netbox_request(path, token, "GET")


def netbox_post(path: str, token: str, body: dict) -> dict:
    return _netbox_request(path, token, "POST", body)


def netbox_patch(path: str, token: str, body: dict) -> dict:
    return _netbox_request(path, token, "PATCH", body)


def netbox_find(path: str, token: str, **filters) -> list:
    """Lijst met resultaten voor een gefilterde GET (limit=0 = alles)."""
    qs = urllib.parse.urlencode({**filters, "limit": 0})
    return netbox_get(f"{path}?{qs}", token)["results"]


def netbox_find_one(path: str, token: str, **filters):
    results = netbox_find(path, token, **filters)
    return results[0] if results else None
