#!/usr/bin/env python3
"""
add_peer.py: interactieve wizard om een nieuwe bilaterale peer toe te voegen.
Vraagt een AS-nummer, zoekt in PeeringDB op welke IX'en die AS aanwezig is,
kruist dat met de IX'en waar deze router zelf op zit (live uit NetBox), en
laat je kiezen op welke overlappende IX('en) je wilt peeren. Configureert
NetBox en triggert optioneel meteen 'generate.py generate'.

Nieuwe peers komen als status=planned/enabled=False te staan - fysiek
aansluiten/activeren blijft een losse, bewuste vervolgstap (generate.py +
activate.py).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from lib.cli import run_main, setup_logging
from lib.config import PEERINGDB_BASE, RELATIONSHIP_IDS
from lib.errors import BgpdGenError
from lib.netbox import load_token, netbox_find, netbox_find_one, netbox_get, netbox_patch, netbox_post
from lib.router import get_router, get_scope

GENERATE_SCRIPT = Path(__file__).parent / "generate.py"


def peeringdb_get(path: str, **params) -> list:
    qs = urllib.parse.urlencode(params)
    url = f"{PEERINGDB_BASE}/{path}?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read())["data"]
    except urllib.error.URLError as e:
        raise BgpdGenError(f"PeeringDB {path} onbereikbaar: {e}") from e


def ask(prompt: str, default=None):
    suffix = f" [{default}]" if default not in (None, "") else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = " [J/n]" if default else " [j/N]"
    val = input(f"{prompt}{suffix}: ").strip().lower()
    if not val:
        return default
    return val in ("j", "ja", "y", "yes")


def discover_ix_fabrics(token: str) -> dict:
    """Leest live uit NetBox welke PeeringFabrics aan een PeeringDB-IX
    gekoppeld zijn (fabric.peeringdb.ix_id, een ingebouwd veld van
    netbox-peering-manager) en welke v4/v6-PeeringNetwork erbij hoort
    (via het IP-versienummer van PeeringNetwork.prefix). Puur een
    afgeleide van NetBox zelf, geen los configbestand - een nieuwe IX
    toevoegen is dus simpelweg een PeeringFabric+PeeringNetwork aanmaken
    in NetBox met de PeeringDB-koppeling gezet."""
    fabrics = netbox_find("/plugins/bgp/peering-fabric/", token)
    networks = netbox_find("/plugins/bgp/peering-network/", token)

    result = {}
    for f in fabrics:
        peeringdb = f.get("peeringdb")
        ix_id = peeringdb.get("ix_id") if peeringdb else None
        if not ix_id:
            continue
        v4 = v6 = None
        for n in networks:
            if n["fabric"]["id"] != f["id"]:
                continue
            version = n["prefix"]["family"]["value"]
            if version == 4:
                v4 = n["name"]
            elif version == 6:
                v6 = n["name"]
        result[str(ix_id)] = {"fabric_name": f["name"], "peering_network_v4": v4, "peering_network_v6": v6}
    return result


def configure_netbox(token: str, asn: int, description: str, comment: str, irr_as_set: str,
                      max4, max6, ipv4, ipv6, fabric_name: str,
                      pn_v4_name: str | None, pn_v6_name: str | None) -> list[str]:
    """Configureert NetBox voor één bilaterale peer op één IX, via de
    read/write REST-API (get-then-create/update, equivalent aan
    Django's get_or_create). Idempotent: opnieuw draaien voor dezelfde
    AS+IX-combinatie wijzigt/dupliceert niets onnodigs.

    Aanname: RIR-slug 'ripe-ncc' (nieuwe ASN-objecten worden daaraan
    gekoppeld) - correct voor een RIPE NCC-lidnetwerk, niet voor ARIN/
    APNIC/LACNIC/AFRINIC. Pas de slug hieronder aan als je in een andere
    regio zit."""
    router = get_router(token)
    local_as = router["asn"]
    scope = get_scope(token)
    address_families = {
        af["address_family"]: af["id"]
        for af in netbox_find("/plugins/routing/bgp/address-family/", token)
        if af["scope"]["id"] == scope["id"]
    }
    ripe = netbox_find_one("/ipam/rirs/", token, slug="ripe-ncc")
    placeholder = netbox_find_one("/ipam/ip-addresses/", token, address="192.0.2.1/32")
    for label, obj in [("RIR (slug 'ripe-ncc')", ripe), ("placeholder-IP 192.0.2.1/32", placeholder)]:
        if obj is None:
            raise BgpdGenError(f"{label} niet gevonden in NetBox - eenmalige set-up ontbreekt")
    bilateral_rel = netbox_get(f"/plugins/bgp/relationship/{RELATIONSHIP_IDS['bilateral-peering']}/", token)

    # ASN: vind op nummer, maak aan indien nodig, werk omschrijving/comment bij
    asn_obj = netbox_find_one("/ipam/asns/", token, asn=asn)
    if asn_obj is None:
        asn_obj = netbox_post("/ipam/asns/", token, {
            "asn": asn, "rir": ripe["id"], "description": description,
        })
    else:
        patch = {}
        if description and asn_obj.get("description") != description:
            patch["description"] = description
        if comment:
            patch["comments"] = comment
        if patch:
            asn_obj = netbox_patch(f"/ipam/asns/{asn_obj['id']}/", token, patch)

    # PeerASN
    peerasn = netbox_find_one("/plugins/bgp/peer-asn/", token, asn_id=asn_obj["id"])
    peerasn_body = {
        "affiliated": False,
        "irr_as_set": irr_as_set or "",
        "ipv4_max_prefixes": max4,
        "ipv6_max_prefixes": max6,
    }
    if peerasn is None:
        peerasn = netbox_post("/plugins/bgp/peer-asn/", token, {"asn": asn_obj["id"], **peerasn_body})
    else:
        patch = {k: v for k, v in peerasn_body.items() if v and peerasn.get(k) != v}
        if patch:
            netbox_patch(f"/plugins/bgp/peer-asn/{peerasn['id']}/", token, patch)

    # Lege, per-AS aanvul-prefixlist (additief bovenop het IRR-filter).
    # Let op: prefix-list?name=... wordt door dit endpoint niet als filter
    # ondersteund (geeft altijd ongefilterd alles terug, zelfde bug als
    # elders) - client-side filteren op de volledige lijst.
    extra_name = f"as{asn}-extra"
    all_prefix_lists = netbox_find("/plugins/routing/objects/prefix-list/", token)
    extra_pl = next((p for p in all_prefix_lists if p["name"] == extra_name), None)
    if extra_pl is None:
        extra_pl = netbox_post("/plugins/routing/objects/prefix-list/", token, {
            "name": extra_name,
            "description": f"Handmatige aanvulling bovenop de IRR-gebaseerde filter voor AS{asn} "
                            f"({description}, bv. RFC1918-uitwisseling). Leeg = geen extra.",
        })

    results = []
    for fam, ip, af_key, pn_name in [
        ("v4", ipv4, "ipv4-unicast", pn_v4_name),
        ("v6", ipv6, "ipv6-unicast", pn_v6_name),
    ]:
        if not ip:
            continue
        peer_name = f"{description} - {fabric_name} ({fam})"

        ip_obj = netbox_find_one("/ipam/ip-addresses/", token, address=ip)
        if ip_obj is None:
            ip_obj = netbox_post("/ipam/ip-addresses/", token, {
                "address": ip, "status": "active",
                "description": f"{description} - bilaterale peering via {fabric_name}",
            })

        # Lookup-sleutel is het IP, niet de naam: dat IP is al uniek per AS+IX
        # (via PeeringDB), dus dit blijft idempotent ook als de naam later
        # wijzigt of bestaande objecten een andere naamconventie gebruiken.
        peer = netbox_find_one("/plugins/routing/bgp/peer/", token, peer_id=ip_obj["id"])
        if peer is None:
            peer = netbox_post("/plugins/routing/bgp/peer/", token, {
                "name": peer_name, "scope": scope["id"], "source": placeholder["id"], "peer": ip_obj["id"],
                "remote_as": asn_obj["id"], "local_as": local_as["id"],
                "enabled": True, "status": "active",
                "description": f"Bilaterale peering met {description} via {fabric_name} "
                                f"(add_peer.py)",
            })

        # assigned_object_type/-id worden door dit endpoint niet als filter
        # ondersteund (geeft altijd ongefilterd alles terug) - client-side
        # filteren op de (kleine) volledige lijst is hier betrouwbaarder.
        all_afs = netbox_find("/plugins/routing/bgp/peer-address-family/", token)
        af = next((a for a in all_afs if a["assigned_object_type"] == "netbox_routing.bgppeer"
                   and a["assigned_object_id"] == peer["id"] and a["address_family"] == address_families[af_key]),
                  None)
        af_id = address_families[af_key]
        if af is None:
            netbox_post("/plugins/routing/bgp/peer-address-family/", token, {
                "assigned_object_type": "netbox_routing.bgppeer", "assigned_object_id": peer["id"],
                "address_family": af_id, "enabled": True, "prefixlist_in": extra_pl["id"],
            })
        elif af.get("prefixlist_in") != extra_pl["id"]:
            netbox_patch(f"/plugins/routing/bgp/peer-address-family/{af['id']}/", token,
                         {"prefixlist_in": extra_pl["id"]})

        pn = netbox_find_one("/plugins/bgp/peering-network/", token, name=pn_name)
        if pn is None:
            raise BgpdGenError(f"PeeringNetwork {pn_name!r} niet gevonden")
        ps = netbox_find_one("/plugins/bgp/peering-session/", token, bgp_peer_id=peer["id"])
        if ps is None:
            netbox_post("/plugins/bgp/peering-session/", token, {
                "bgp_peer": peer["id"], "relationship": bilateral_rel["id"],
                "service_reference": f"{fabric_name} bilaterale peering (add_peer.py, AS{asn})",
                "peering_network": pn["id"],
            })
        elif ps.get("peering_network", {}).get("id") != pn["id"]:
            netbox_patch(f"/plugins/bgp/peering-session/{ps['id']}/", token, {"peering_network": pn["id"]})

        results.append(peer_name)

    return results


def cmd_add_peer(args: argparse.Namespace) -> None:
    token = load_token()

    ix_fabrics = discover_ix_fabrics(token)
    if not ix_fabrics:
        raise BgpdGenError(
            "geen enkele PeeringFabric in NetBox heeft een PeeringDB-koppeling "
            "(fabric.peeringdb) - zet die eerst voordat je add_peer.py gebruikt"
        )

    asn_input = ask("Welk AS-nummer wil je toevoegen?")
    if not asn_input or not str(asn_input).isdigit():
        raise BgpdGenError("ongeldig AS-nummer")
    asn = int(asn_input)

    print(f"\nPeeringDB opzoeken voor AS{asn}...")
    nets = peeringdb_get("net", asn=asn)
    if not nets:
        raise BgpdGenError(f"geen PeeringDB-net gevonden voor AS{asn}")
    net = nets[0]
    peeringdb_name = net.get("name") or f"AS{asn}"
    irr_as_set = net.get("irr_as_set") or ""
    max4 = net.get("info_prefixes4") or None
    max6 = net.get("info_prefixes6") or None
    print(f"Gevonden: {peeringdb_name} (PeeringDB net {net['id']})")
    if not irr_as_set:
        print("  LET OP: geen irr_as_set bij deze net geregistreerd in PeeringDB - "
              "dynamische IRR-filtering zal geen routes doorlaten totdat je dit handmatig aanvult.")

    netixlans = peeringdb_get("netixlan", asn=asn)
    if not netixlans:
        raise BgpdGenError(f"AS{asn} staat op geen enkele IX in PeeringDB")

    matches = [n for n in netixlans if str(n["ix_id"]) in ix_fabrics]
    if not matches:
        print(f"\nGeen overlap met IX'en waar deze router op zit. AS{asn} is aanwezig op:")
        for n in netixlans:
            print(f"  - {n.get('name', n['ix_id'])} (ix_id {n['ix_id']})")
        print(f"Bekende fabrics (uit NetBox): {list(ix_fabrics.keys())}")
        return

    print(f"\nOverlappende IX('en) gevonden voor AS{asn}:")
    for i, m in enumerate(matches, 1):
        fabric = ix_fabrics[str(m["ix_id"])]["fabric_name"]
        print(f"  {i}. {m.get('name', m['ix_id'])} -> NetBox-fabric '{fabric}' "
              f"(v4: {m.get('ipaddr4') or '-'}, v6: {m.get('ipaddr6') or '-'})")

    selection = ask("Welke wil je configureren? (bv. '1' of '1,2', Enter = geen)", default="")
    if not selection:
        print("Niets geselecteerd, stoppen.")
        return
    indices = [int(x.strip()) for x in selection.split(",") if x.strip().isdigit()]
    chosen = [matches[i - 1] for i in indices if 1 <= i <= len(matches)]
    if not chosen:
        raise BgpdGenError("geen geldige selectie")

    description = ask("Omschrijving voor dit AS (naam zoals jij hem kent)", default=peeringdb_name)
    comment = ask("Contactpersoon (optioneel, Enter om over te slaan)", default="")
    if not irr_as_set:
        irr_as_set = ask("Geen irr_as_set gevonden - handmatig invullen? (optioneel)", default="")
    max4_in = ask("Max-prefix IPv4 (Enter = PeeringDB-waarde)", default=max4)
    max6_in = ask("Max-prefix IPv6 (Enter = PeeringDB-waarde)", default=max6)
    max4 = int(max4_in) if max4_in is not None else None
    max6 = int(max6_in) if max6_in is not None else None

    for m in chosen:
        fabric_info = ix_fabrics[str(m["ix_id"])]
        print(f"\n=== {m.get('name', m['ix_id'])} -> {fabric_info['fabric_name']} ===")
        print(f"  AS{asn} ({description}), IRR: {irr_as_set or '(geen)'}, "
              f"max-prefix v4={max4} v6={max6}")
        print(f"  v4: {m.get('ipaddr4') or '(niet aanwezig)'}  v6: {m.get('ipaddr6') or '(niet aanwezig)'}")
        if not ask_yes_no("Doorvoeren in NetBox?", default=True):
            print("  Overgeslagen.")
            continue
        if not fabric_info.get("peering_network_v4") and m.get("ipaddr4"):
            print("  WAARSCHUWING: geen IPv4-PeeringNetwork bekend voor deze fabric - v4-sessie overgeslagen.")
        if not fabric_info.get("peering_network_v6") and m.get("ipaddr6"):
            print("  WAARSCHUWING: geen IPv6-PeeringNetwork bekend voor deze fabric - v6-sessie overgeslagen.")
        created = configure_netbox(
            token, asn=asn, description=description, comment=comment,
            irr_as_set=irr_as_set, max4=max4, max6=max6,
            ipv4=m.get("ipaddr4") if fabric_info.get("peering_network_v4") else None,
            ipv6=m.get("ipaddr6") if fabric_info.get("peering_network_v6") else None,
            fabric_name=fabric_info["fabric_name"],
            pn_v4_name=fabric_info.get("peering_network_v4"),
            pn_v6_name=fabric_info.get("peering_network_v6"),
        )
        print("  KLAAR:", ", ".join(created) if created else "(niets - geen matchende adresfamilie)")

    if ask_yes_no("\nConfiguratie nu regenereren (fetch/bgpq4/render)?", default=True):
        print("\n==> Configuratie regenereren (generate.py generate)...")
        result = subprocess.run([sys.executable, str(GENERATE_SCRIPT), "generate"])
        if result.returncode != 0:
            raise BgpdGenError(f"generate.py generate gaf exitcode {result.returncode}")
        print(f"\nKlaar. Bekijk output/ en draai handmatig "
              f"'sudo python3 {Path(__file__).parent / 'activate.py'}' wanneer je zover bent.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="add_peer.py",
        description="Interactief: nieuwe bilaterale peer toevoegen via PeeringDB.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug-logging")
    args = parser.parse_args()
    args.log = setup_logging(args.verbose, "add-peer")
    run_main(lambda: cmd_add_peer(args), args.log)


if __name__ == "__main__":
    main()
