"""Lookups die vastzitten aan de ene router die dit script-geheel bedient
(config.ROUTER_ID): het NetBox BGPRouter-object zelf, de bijbehorende
BGPScope, het gekoppelde Device-id en het eigen AS-nummer. Gedeeld door elk
script dat weet moet hebben van "welke router/AS zijn wij eigenlijk"
(generate.py, add_peer.py, render_irr_policy.py, verify_protection.py) - één
bron van waarheid i.p.v. een los hardcoded AS-nummer per script."""
from __future__ import annotations

from lib.config import ROUTER_ID
from lib.errors import BgpdGenError
from lib.netbox import netbox_find, netbox_get


def get_router(token: str) -> dict:
    return netbox_get(f"/plugins/routing/bgp/router/{ROUTER_ID}/", token)


def get_scope(token: str) -> dict:
    """De ene BGPScope die bij ROUTER_ID hoort. OpenBGPD op Linux (deze
    router draait openbgpd-portable, niet OpenBSD) kent geen VRF/rdomain-
    ondersteuning zoals OpenBSD dat heeft - één bgpd-proces bedient precies
    één routeringstabel. Een router met meerdere scopes zou dus op iets
    onverwachts wijzen (bv. handmatig aangemaakte tweede VRF-scope); faal
    dan expliciet in plaats van stilzwijgend de eerste te pakken."""
    scopes = netbox_find("/plugins/routing/bgp/scope/", token, router_id=ROUTER_ID)
    if not scopes:
        raise BgpdGenError(f"geen BGPScope gevonden voor router {ROUTER_ID}")
    if len(scopes) > 1:
        names = ", ".join(f"{s['id']} ({s.get('vrf', {}).get('name', 'geen VRF')})" for s in scopes)
        raise BgpdGenError(
            f"router {ROUTER_ID} heeft {len(scopes)} BGPScopes ({names}) - "
            f"niet ondersteund: openbgpd-portable (Linux) kent geen VRF's/rdomains, "
            f"dus welke scope bedoeld is, is niet af te leiden. Verwijder de overtollige "
            f"scope(s) of splits dit op in een aparte BGPRouter per scope."
        )
    return scopes[0]


def device_id_from_router(router: dict) -> int:
    """Leidt het NetBox Device-id af uit een reeds-opgehaald BGPRouter
    (assigned_object) - nodig voor primary_ip4 (router-id) en om StaticRoute-
    objecten (eigen prefixes) aan dit device te koppelen."""
    assigned = router.get("assigned_object")
    if not assigned or "/dcim/devices/" not in assigned.get("url", ""):
        raise BgpdGenError(
            f"BGPRouter {ROUTER_ID} is niet aan een Device gekoppeld "
            f"(assigned_object={assigned}) - kan zo geen router-id/eigen-prefixes afleiden"
        )
    return assigned["id"]


def get_local_asn(token: str) -> int:
    """Het eigen AS-nummer, rechtstreeks uit NetBox (BGPRouter.asn) i.p.v.
    een los hardcoded getal per script - zelfde bron als build_context()
    (generate.py) en render_irr_policy.py gebruiken."""
    return get_router(token)["asn"]["asn"]
