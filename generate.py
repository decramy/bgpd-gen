#!/usr/bin/env python3
"""
generate.py: genereert de volledige OpenBGPD-configuratie voor deze router
rechtstreeks vanuit NetBox. Uitsluitend generatie - activeren gebeurt met
het losse activate.py, nieuwe peers toevoegen met add_peer.py, RIPE-beleid
opvragen met render_irr_policy.py. Zie ../README.md voor de volledige
architectuur.

Subcommando's:
    fetch-as-sets           stap 1: irr_as_set per bilaterale peer -> as-sets.txt
    generate-prefix-sets    stap 2: bgpq4 per AS -> output/peers/as<asn>.conf
    generate-prefix-lists   stap 3: NetBox PrefixList(Entry) -> output/peers/prefix-lists.conf
    render-main-config      stap 4: templates/*.j2 renderen -> output/bgpd.conf + output/peers/*.conf
    generate                 stap 1 t/m 4 in volgorde (dit is wat de cronjob aanroept)

Dependencies: python3 + python3-jinja2 (apt) + bgpq4. Configuratie in
lib/config.py, gedeelde NetBox/bgpctl-code in lib/.
"""
from __future__ import annotations

import argparse
import ipaddress
import shutil
import subprocess
import time
import urllib.parse

import jinja2

from lib.cli import run_main, setup_logging
from lib.config import AS_SETS_FILE, NETBOX_BASE, OUTPUT_DIR, PEERS_DIR, RELATIONSHIP_IDS, TEMPLATES_DIR
from lib.errors import BgpdGenError
from lib.netbox import load_token, netbox_get
from lib.router import device_id_from_router, get_router, get_scope
from lib.sessions import build_sessions
from lib.util import atomic_write

BGPD_CONF_TEMPLATE = "bgpd.conf.j2"
PEER_GROUP_TEMPLATE = "peer-group.conf.j2"


# --- stap 1: as-sets ophalen -------------------------------------------------

def cmd_fetch_as_sets(args: argparse.Namespace) -> None:
    """Haalt de irr_as_set-waarden op voor ASNs die via een Bilaterale
    Peering-sessie gekoppeld zijn, en schrijft ze naar as-sets.txt als
    "<asn> <as-set>" per regel.

    Bewust beperkt tot Bilateral Peering: transit- en route-server-sessies
    filteren via handmatig beheerde NetBox-prefixlisten (default-only/
    full-table), niet via een IRR-macro - hun as-sets kunnen bovendien
    enorm zijn (tientallen MB's aan bgpq4-output voor een grote IX).
    """
    token = load_token()
    args.log.info("as-sets ophalen uit NetBox...")
    sessions = netbox_get("/plugins/bgp/peering-session/?limit=0", token)["results"]
    peerasns = netbox_get("/plugins/bgp/peer-asn/?limit=0", token)["results"]

    bilateral_asns = {
        r["bgp_peer"]["remote_as"]["asn"]
        for r in sessions
        if (r.get("relationship") or {}).get("id") == RELATIONSHIP_IDS["bilateral-peering"]
        and r["bgp_peer"].get("remote_as")
    }

    lines = []
    for pa in peerasns:
        asn = (pa.get("asn") or {}).get("asn")
        irr = pa.get("irr_as_set")
        if asn in bilateral_asns and irr:
            lines.append(f"{asn} {irr}")

    # Succesvolle fetch overschrijft altijd, ook als er (nu) geen enkele
    # bilaterale peer is - dat is een legitieme staat, geen fout.
    content = "\n".join(lines) + ("\n" if lines else "")
    atomic_write(AS_SETS_FILE, content)
    args.log.info("OK: %d as-set(s) opgehaald uit NetBox", len(lines))


# --- stap 2: bgpq4 -----------------------------------------------------------

def cmd_generate_prefix_sets(args: argparse.Namespace) -> None:
    """Genereert per bilaterale peer één macrobestand (output/peers/as<asn>.conf)
    met zowel de IPv4- als IPv6-prefix-set-macro erin. Bestandsnaam en
    macronaam zijn op het AS-nummer gebaseerd, niet op de as-set-naam of de
    peer zelf - de peer noemt zichzelf nergens in de configuratie, het
    AS-nummer is altijd ondubbelzinnig (en voorkomt duplicatie als dezelfde
    peer ooit op meerdere IX'en gevonden wordt)."""
    if shutil.which("bgpq4") is None:
        raise BgpdGenError("bgpq4 niet gevonden in PATH - installeer het pakket eerst")
    if not AS_SETS_FILE.exists():
        raise BgpdGenError(f"{AS_SETS_FILE} ontbreekt - draai eerst 'fetch-as-sets'")

    args.log.info("IRR-prefix-sets genereren met bgpq4...")
    for lineno, raw in enumerate(AS_SETS_FILE.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            asn, asset = line.split(None, 1)
        except ValueError:
            args.log.warning("as-sets.txt regel %d overgeslagen (onverwacht formaat): %r", lineno, raw)
            continue

        outfile = PEERS_DIR / f"as{asn}.conf"
        parts = []
        ok = True
        for fam in (4, 6):
            # Macronamen mogen bij bgpd alleen alfanumeriek + '_' bevatten
            # (geen '-') - vandaar underscores, niet de streepjes-variant.
            listname = f"prefix_set_as{asn}_v{fam}"
            try:
                result = subprocess.run(
                    ["bgpq4", "-B", "-l", listname, f"-{fam}", asset],
                    capture_output=True, text=True, timeout=60,
                )
            except subprocess.TimeoutExpired:
                args.log.error("FOUT: bgpq4 timeout voor AS%s (%s, v%d)", asn, asset, fam)
                ok = False
                continue
            if result.returncode != 0:
                args.log.error("FOUT: bgpq4 mislukt voor AS%s (%s, v%d): %s",
                                asn, asset, fam, result.stderr.strip())
                ok = False
                continue
            if "is empty" in result.stdout:
                # Een lege 'prefix { }'-clausule is zelf ongeldige bgpd-syntax
                # (moet minstens één prefix bevatten) - macro dan overslaan.
                # Sessies gebruiken toch alleen de macro van hun eigen
                # adresfamilie (op basis van remote_ip), dus dit is veilig.
                args.log.info("AS%s (%s) heeft geen v%d-prefixes - macro overgeslagen", asn, asset, fam)
                continue
            parts.append(result.stdout)
            args.log.info("OK: AS%s (%s, v%d) -> %s", asn, asset, fam, outfile.name)

        if ok:
            atomic_write(outfile, "".join(parts))
        else:
            args.log.warning("%s NIET bijgewerkt (oud bestand blijft staan indien aanwezig)", outfile.name)


# --- stap 3: beheerde prefix-lists -------------------------------------------

def cmd_generate_prefix_lists(args: argparse.Namespace) -> None:
    """Haalt alle NetBox PrefixList/PrefixListEntry-objecten (netbox-routing)
    op en rendert ze als 'prefix-set "naam" { ... }'-blokken.

    NetBox's PrefixList/PrefixListEntry-objecten bevatten alleen de rauwe
    data (prefix/ge/le/sequence), geen bgpd-syntax - zonder deze stap zouden
    "default-only"/"full-table"/de per-peer aanvul-lijsten wel gerefereerd
    worden in bgpd.conf, maar nooit gedefinieerd."""
    token = load_token()
    args.log.info("beheerde prefix-lists genereren...")
    lists = netbox_get("/plugins/routing/objects/prefix-list/?limit=0", token)["results"]
    entries = netbox_get("/plugins/routing/objects/prefix-list-entry/?limit=0", token)["results"]

    by_list: dict[int, list] = {}
    for e in entries:
        if e["action"] != "permit":
            args.log.warning("entry %s in %s heeft action=%r, overgeslagen "
                              "(alleen permit wordt ondersteund in een platte prefix-set)",
                              e["id"], e["prefix_list"]["name"], e["action"])
            continue
        by_list.setdefault(e["prefix_list"]["id"], []).append(e)

    out = ["# Gegenereerd uit NetBox (netbox-routing PrefixList/PrefixListEntry) - niet handmatig bewerken.", ""]
    for pl in sorted(lists, key=lambda x: x["name"]):
        rows = sorted(by_list.get(pl["id"], []), key=lambda e: e["sequence"])
        out.append(f"# {pl['name']}: {pl.get('description', '')}")
        out.append(f'prefix-set "{pl["name"]}" {{')
        parts = []
        for e in rows:
            prefix = e["assigned_prefix"]["prefix"]
            le = e.get("le") or 0
            ge = e.get("ge") or 0
            # Let op: de "-" rangeoperator vereist spaties eromheen in bgpd.conf
            # ("0 - 32", niet "0-32") - zonder spaties geeft bgpd een kale syntax error.
            parts.append(f"\t{prefix} prefixlen {ge} - {le}" if le else f"\t{prefix}")
        out.append(",\n".join(parts))
        out.append("}")
        out.append("")

    atomic_write(PEERS_DIR / "prefix-lists.conf", "\n".join(out) + "\n")
    args.log.info("OK: prefix-lists.conf geschreven (%d lijst(en))", len(lists))


# --- stap 4: hoofdtemplate renderen ------------------------------------------
#
# Rendert lokaal met Jinja2 (templates/*.j2), niet via NetBox's ConfigTemplate/
# render-config-API: die context is vast (alleen device+sessions, geen
# StaticRoute/primary_ip4/generatiemetadata) en niet uitbreidbaar zonder de
# plugin-broncode aan te passen, wat we bewust niet doen. Alle opmaak/
# structuur staat daarom in de templates zelf; dit script levert alleen de
# platte data (device/sessions/eigen prefixes) via REST-joins.

def _own_prefixes(token: str, device_id: int) -> list[str]:
    """Eigen te originaten prefixes uit NetBox (netbox-routing StaticRoute,
    gekoppeld aan dit Device). Nieuwe eigen prefixes toevoegen = een
    StaticRoute aanmaken in NetBox en aan dit Device koppelen, geen
    template- of scriptaanpassing nodig."""
    routes = netbox_get("/plugins/routing/routes/static/?limit=0", token)["results"]
    own = sorted(
        (r["prefix"] for r in routes if any(d["id"] == device_id for d in r.get("devices", []))),
    )
    if not own:
        raise BgpdGenError(
            f"geen StaticRoute in NetBox gekoppeld aan device #{device_id} - "
            f"bgpd zou dan niets hebben om te originaten/aankondigen"
        )
    return own


def _connected_networks(token: str, device_id: int) -> list[str]:
    """Alle netwerken die rechtstreeks aan dit Device hangen (via een
    IPAddress op een Interface), als netwerk-CIDR (bv. '192.0.2.11/24'
    -> '192.0.2.0/24'). Voorkomt dat bgpd een BGP-geleerde route voor
    zo'n zelfde netwerk (bv. de peering-LAN-prefix die een upstream legitiem
    meestuurt in de volle tabel) de kernel's eigen connected-route laat
    overschrijven - fib-update installeert die BGP-route anders via een
    gateway i.p.v. de directe interface-route, waardoor buren op datzelfde
    LAN (incl. de BGP-peers zelf) alleen nog indirect/circulair bereikbaar
    zijn. Volledig dynamisch: een interface/IP toevoegen of wijzigen in
    NetBox is genoeg, geen script- of templateaanpassing nodig."""
    addrs = netbox_get(f"/ipam/ip-addresses/?device_id={device_id}&limit=0", token)["results"]
    networks = {str(ipaddress.ip_interface(a["address"]).network) for a in addrs}
    return sorted(networks)


def _group_key(session: dict) -> str:
    pn = session.get("peering_network")
    return pn["fabric"] if pn else session["peer_name"]


def build_context(token: str) -> tuple[dict, list[str]]:
    """Bouwt de volledige Jinja-context voor deze router: device, sessions,
    eigen prefixes en herkomst-/versheidsmetadata (welke NetBox, welke
    router/device, wanneer gegenereerd) - alles wat vroeger via een losse
    regex-substitutie of tekst-prepend in Python geplakt werd, is nu een
    normale contextvariabele."""
    router = get_router(token)
    device_id = device_id_from_router(router)
    device = netbox_get(f"/dcim/devices/{device_id}/", token)
    primary_ip4 = (device.get("primary_ip4") or {}).get("address")
    if not primary_ip4:
        raise BgpdGenError(f"Device #{device_id} heeft geen primary_ip4 in NetBox - nodig voor router-id")

    scope = get_scope(token)
    sessions = build_sessions(token, scope["id"])
    groups = sorted({_group_key(s) for s in sessions})

    return {
        "device": {"name": device["name"], "site": device.get("site")},
        "sessions": sessions,
        "own_prefixes": _own_prefixes(token, device_id),
        "connected_networks": _connected_networks(token, device_id),
        "groups": [g.lower().replace(" ", "-") for g in groups],
        "local_asn": router["asn"]["asn"],
        "router_id_ip": primary_ip4.split("/")[0],
        "netbox_host": urllib.parse.urlparse(NETBOX_BASE).netloc,
        "router_name": router.get("name") or f"BGPRouter #{router['id']}",
        "router_netbox_id": router["id"],
        "device_netbox_id": device_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S %Z", time.localtime()),
    }, groups


def cmd_render_main_config(args: argparse.Namespace) -> None:
    """Rendert bgpd.conf en één peers/<groep>.conf per fabric/IX-groep met
    de lokale Jinja2-templates onder templates/, met een context die dit
    script zelf via REST-joins opbouwt (build_context)."""
    token = load_token()
    args.log.info("hoofdconfig + peer-groepen renderen (lokale templates)...")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES_DIR), keep_trailing_newline=True)
    context, groups = build_context(token)

    main_content = env.get_template(BGPD_CONF_TEMPLATE).render(context)
    atomic_write(OUTPUT_DIR / "bgpd.conf", main_content.strip() + "\n")
    args.log.info("geschreven: bgpd.conf")

    peer_group_tpl = env.get_template(PEER_GROUP_TEMPLATE)
    for g in groups:
        slug = g.lower().replace(" ", "-")
        group_sessions = [s for s in context["sessions"] if _group_key(s) == g]
        content = peer_group_tpl.render(group_name=g, sessions=group_sessions)
        atomic_write(OUTPUT_DIR / "peers" / f"{slug}.conf", content.strip() + "\n")
        args.log.info("geschreven: peers/%s.conf", slug)

    args.log.info("KLAAR: %d bestand(en)", 1 + len(groups))


# --- orchestrator -------------------------------------------------------------

def cmd_generate(args: argparse.Namespace) -> None:
    cmd_fetch_as_sets(args)
    cmd_generate_prefix_sets(args)
    cmd_generate_prefix_lists(args)
    cmd_render_main_config(args)
    args.log.info("KLAAR: complete configuratie staat in %s", OUTPUT_DIR)


# --- CLI ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="generate.py",
        description="NetBox-gedreven OpenBGPD-configuratiegeneratie voor deze router (alleen generatie, zie activate.py/add_peer.py/render_irr_policy.py voor de rest).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug-logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch-as-sets", help="stap 1: irr_as_set per bilaterale peer -> as-sets.txt") \
        .set_defaults(func=cmd_fetch_as_sets)
    sub.add_parser("generate-prefix-sets", help="stap 2: bgpq4 -> output/peers/as<asn>.conf") \
        .set_defaults(func=cmd_generate_prefix_sets)
    sub.add_parser("generate-prefix-lists", help="stap 3: NetBox PrefixList(Entry) -> output/peers/prefix-lists.conf") \
        .set_defaults(func=cmd_generate_prefix_lists)
    sub.add_parser("render-main-config", help="stap 4: hoofdtemplate + peer-groepen renderen") \
        .set_defaults(func=cmd_render_main_config)
    sub.add_parser("generate", help="stap 1 t/m 4, voor cron") \
        .set_defaults(func=cmd_generate)

    args = parser.parse_args()
    args.log = setup_logging(args.verbose, "generate")
    run_main(lambda: args.func(args), args.log)


if __name__ == "__main__":
    main()
