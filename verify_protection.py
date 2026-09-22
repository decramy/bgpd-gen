#!/usr/bin/env python3
"""Verifieert of een BGP-neighbor daadwerkelijk RTBH en/of Flowspec-regels
honoreert - niet alleen of ze het claimen te ondersteunen.

Onderdeel van hetzelfde script-geheel als generate.py/activate.py/
add_peer.py/render_irr_policy.py: canary-adressen staan in lib/config.py
(de enige plek om per omgeving aan te passen) en het eigen AS-nummer wordt,
net als in generate.py's build_context() en render_irr_policy.py, live uit
NetBox gehaald (lib/router.py) i.p.v. hier nogmaals los hardcoded te staan.
Gebruikt verder uitsluitend dynamische 'bgpctl network/flowspec add/delete'-
commando's (lib/bgpctl.py); raakt de door generate.py/activate.py beheerde
gegenereerde config niet aan.

De output is bewust als checklist opgezet: bij een FAIL is de uitleg
zelfstandig leesbaar, zodat je 'm zonder verdere toelichting kunt
doorsturen naar de technische contactpersoon van de geteste peer.
"""
from __future__ import annotations

import argparse
import ipaddress
import shutil
import subprocess
import sys
import time

from lib.bgpctl import bgpctl, bgpctl_json
from lib.config import CANARY_V4, CANARY_V6
from lib.errors import BgpdGenError
from lib.netbox import load_token
from lib.router import get_local_asn

RTBH_COMMUNITY = "65535:666"  # RFC 7999, well-known BLACKHOLE - geen configuratie, is een standaard
PING_COUNT = 5
PROPAGATION_WAIT = 5  # seconden - ruim genoeg voor een directe bilaterale sessie
FALLBACK_PREFIX_LIMIT = 3


class Status:
    OK = "OK"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


class Checklist:
    """Verzamelt en print checklist-items in een vorm die zonder verdere
    toelichting doorgestuurd kan worden naar een externe contactpersoon."""

    def __init__(self, title):
        self.title = title
        self.failed = False
        print(f"=== {title} ===\n")

    def add(self, label, status, detail=None):
        print(f"[{status}] {label}")
        if detail:
            for line in detail.strip().splitlines():
                print(f"       {line}")
        print()
        if status == Status.FAIL:
            self.failed = True
        return status


# --- eigen AS-nummer, lazy + gecached: alleen nodig voor de flowspec-test
# (flow-rate ext-community), dus een RTBH-only run blijft onafhankelijk van
# NetBox-bereikbaarheid - zelfde locatie-onafhankelijkheidsprincipe als de
# rest van dit project hanteert.

_local_asn_cache: int | None = None


def local_asn() -> int:
    global _local_asn_cache
    if _local_asn_cache is None:
        _local_asn_cache = get_local_asn(load_token())
    return _local_asn_cache


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def ping_ok(target, family, source=None, count=PING_COUNT):
    # Aanname: Linux/iputils-ping. Geen OpenBSD-ondersteuning (andere ping/ping6-flags) - bewuste keuze, niet opgelost.
    if family == 6:
        cmd = ["ping6"] if shutil.which("ping6") else ["ping", "-6"]
    else:
        cmd = ["ping", "-4"]
    cmd += ["-c", str(count), "-W", "2"]
    if source:
        cmd += ["-I", source]
    cmd.append(target)
    return run(cmd).returncode == 0


def negotiated_flowspec(neighbor_ip):
    """Kijkt specifiek naar 'Negotiated capabilities', niet naar
    'Neighbor capabilities' - dat laatste is alleen wat de peer aanbiedt,
    los van of onze eigen kant het ook aanbiedt."""
    out = bgpctl("show", "neighbor", neighbor_ip)
    if "Negotiated capabilities:" not in out:
        return False
    block = out.split("Negotiated capabilities:", 1)[1]
    return "flowspec" in block.lower()


def session_established(neighbor_ip):
    out = bgpctl("show", "neighbor", neighbor_ip)
    return "BGP state = Established" in out


def received_prefixes(neighbor_ip, limit=FALLBACK_PREFIX_LIMIT):
    data = bgpctl_json("show", "rib", "neighbor", neighbor_ip)
    return [entry["prefix"] for entry in data.get("rib", [])][:limit]


def candidate_hosts(prefix):
    """.1 en het laatste bruikbare adres binnen een ontvangen prefix -
    snelle, niet-uitputtende manier om een levende host te vinden zonder
    een heel blok te scannen. Prefixes langer dan /30 (v4) of /126 (v6)
    hebben geen bruikbare tweede kandidaat en worden overgeslagen."""
    net = ipaddress.ip_network(prefix, strict=False)
    max_len = 30 if net.version == 4 else 126
    if net.prefixlen > max_len:
        return []
    hosts = list(net.hosts())
    if not hosts:
        return []
    candidates = [str(hosts[0])]
    if hosts[-1] != hosts[0]:
        candidates.append(str(hosts[-1]))
    return candidates


def find_live_host(neighbor_ip, family):
    for prefix in received_prefixes(neighbor_ip):
        net = ipaddress.ip_network(prefix, strict=False)
        if (net.version == 4) != (family == 4):
            continue
        for host in candidate_hosts(prefix):
            if ping_ok(host, family, count=2):
                return host, prefix
    return None, None


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def preflight_openbgpd(cl):
    if not shutil.which("bgpctl"):
        cl.add(
            "OpenBGPD gevonden en bereikbaar", Status.FAIL,
            "Het 'bgpctl'-commando is niet gevonden op dit systeem. Dit script "
            "gebruikt OpenBGPD-specifieke syntax en heeft op een ander platform "
            "(FRR, BIRD, een vendor-router) geen betekenis.",
        )
        return False
    r = run(["bgpctl", "show", "summary"])
    if r.returncode != 0:
        cl.add(
            "OpenBGPD gevonden en bereikbaar", Status.FAIL,
            "'bgpctl' bestaat, maar 'bgpctl show summary' gaf een fout terug "
            f"({r.stderr.strip() or 'geen output'}). Draait bgpd, en heeft dit "
            "account toegang tot het control-socket?",
        )
        return False
    cl.add("OpenBGPD gevonden en bereikbaar", Status.OK)
    return True


def preflight_session(cl, neighbor_ip):
    try:
        established = session_established(neighbor_ip)
    except BgpdGenError as e:
        cl.add(f"Sessie met {neighbor_ip} is Established", Status.FAIL, str(e))
        return False
    if not established:
        cl.add(
            f"Sessie met {neighbor_ip} is Established", Status.FAIL,
            f"De BGP-sessie met {neighbor_ip} staat niet in state Established. "
            "Elke ping-uitkomst hieronder is dan betekenisloos - eerst de sessie "
            "zelf herstellen voordat RTBH/Flowspec te testen is.",
        )
        return False
    cl.add(f"Sessie met {neighbor_ip} is Established", Status.OK)
    return True


def preflight_canary(cl, canary, family):
    if not canary:
        cl.add(
            f"Canary-adres geconfigureerd voor IPv{family}", Status.FAIL,
            f"CANARY_V{family} staat niet ingevuld in lib/config.py - er is dus "
            "geen testadres voor dit address family.",
        )
        return False
    if not ping_ok(canary, family, count=2):
        cl.add(
            f"Canary lokaal aanwezig ({canary})", Status.FAIL,
            f"Het testadres {canary} is zelf niet pingbaar. Waarschijnlijk is de "
            "eenmalige prerequisite-setup (loopback-binding, IRR-object, RPKI-ROA) "
            "nog niet gedaan - zie de documentatie bij dit script.",
        )
        return False
    cl.add(f"Canary lokaal aanwezig ({canary})", Status.OK)
    return True


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def rtbh_test(cl, neighbor_ip, canary, family):
    canary_pfx = f"{canary}/{32 if family == 4 else 128}"

    if not ping_ok(neighbor_ip, family, source=canary):
        cl.add(
            "RTBH", Status.SKIP,
            f"Baseline-ping van {canary} naar {neighbor_ip} kwam al niet aan, "
            "los van RTBH. Geen conclusie over RTBH mogelijk.",
        )
        return

    bgpctl("network", "add", canary_pfx, "set", "community", RTBH_COMMUNITY)
    try:
        time.sleep(PROPAGATION_WAIT)
        still_reachable = ping_ok(neighbor_ip, family, source=canary)
    finally:
        bgpctl("network", "delete", canary_pfx)

    if still_reachable:
        cl.add(
            "RTBH", Status.FAIL,
            f"Verwacht: verkeer van {canary} naar jullie kant stopt zodra de route "
            f"naar {canary_pfx} de RFC 7999 BLACKHOLE-community ({RTBH_COMMUNITY}) "
            f"draagt. Waargenomen: na {PROPAGATION_WAIT}s propagatietijd kwam de "
            "ping gewoon aan. Kunnen jullie nakijken of deze community op jullie "
            "edge-router richting deze sessie herkend en toegepast wordt?",
        )
    else:
        cl.add("RTBH", Status.OK, "Verkeer viel weg zodra de RTBH-community actief was.")


def flowspec_test(cl, neighbor_ip, canary, family):
    canary_pfx = f"{canary}/{32 if family == 4 else 128}"
    fs_family = "inet" if family == 4 else "inet6"

    if not negotiated_flowspec(neighbor_ip):
        cl.add(
            "Flowspec-capability onderhandeld", Status.WARN,
            "'flowspec' komt niet voor in de Negotiated capabilities van deze "
            "sessie. Wij bieden de capability aan; als de andere kant 'm ook "
            "ondersteunt, controleer dan of AFI/SAFI flowspec daadwerkelijk "
            "aan staat voor specifiek deze sessie.",
        )
        return
    cl.add("Flowspec-capability onderhandeld", Status.OK)

    if not ping_ok(neighbor_ip, family, source=canary):
        cl.add(
            "Flowspec", Status.SKIP,
            f"Baseline-ping van {canary} naar {neighbor_ip} kwam al niet aan, "
            "los van flowspec. Geen conclusie mogelijk.",
        )
        return

    try:
        asn = local_asn()
    except BgpdGenError as e:
        cl.add(
            "Flowspec", Status.SKIP,
            f"Eigen AS-nummer kon niet uit NetBox opgehaald worden ({e}) - nodig "
            "voor de flow-rate ext-community. Geen conclusie mogelijk.",
        )
        return

    bgpctl("flowspec", "add", fs_family, "to", canary_pfx,
           "set", "ext-community", "flow-rate", f"{asn}:0")
    try:
        time.sleep(PROPAGATION_WAIT)
        still_reachable = ping_ok(neighbor_ip, family, source=canary)
    finally:
        bgpctl("flowspec", "delete", fs_family, "to", canary_pfx)

    if not still_reachable:
        cl.add("Flowspec", Status.OK, "Verkeer viel weg zodra de flowspec drop-rule actief was.")
        return

    cl.add(
        "Flowspec (self-reply-test)", Status.WARN,
        f"Ping naar {neighbor_ip} zelf kwam na de drop-rule nog steeds aan. Dit "
        "kan een vals-negatief zijn: de rule wordt mogelijk wel toegepast op "
        "doorgeroute transitverkeer, maar niet op verkeer dat de router van de "
        "peer zelf genereert (zoals een ICMP-reply). Onderstaande fallback "
        "test dit met een echte host achter de peer.",
    )
    flowspec_fallback(cl, neighbor_ip, canary, canary_pfx, fs_family, family, asn)


def flowspec_fallback(cl, neighbor_ip, canary, canary_pfx, fs_family, family, asn):
    host, via_prefix = find_live_host(neighbor_ip, family)
    if not host:
        cl.add(
            "Flowspec (fallback, echte host)", Status.WARN,
            "Geen levende host gevonden in de eerste ontvangen prefixes van deze "
            "neighbor. De self-reply-uitkomst hierboven blijft het enige "
            "beschikbare signaal - flowspec-ondersteuning is dus niet hard "
            "aangetoond, maar ook niet uitgesloten.",
        )
        return

    if not ping_ok(host, family, source=canary):
        cl.add(
            "Flowspec (fallback, echte host)", Status.WARN,
            f"{host} (uit {via_prefix}) leek eerst pingbaar maar de baseline "
            f"vanaf {canary} kwam niet aan. Geen conclusie via deze route.",
        )
        return

    bgpctl("flowspec", "add", fs_family, "to", canary_pfx,
           "set", "ext-community", "flow-rate", f"{asn}:0")
    try:
        time.sleep(PROPAGATION_WAIT)
        still_reachable = ping_ok(host, family, source=canary)
    finally:
        bgpctl("flowspec", "delete", fs_family, "to", canary_pfx)

    if still_reachable:
        cl.add(
            "Flowspec (fallback, echte host)", Status.FAIL,
            f"Ook via een echte host ({host}, uit {via_prefix} - dus met normaal "
            "doorgerouteerd transitverkeer, niet zelf-gegenereerd door jullie "
            "router) bleef de ping na de drop-rule gewoon aankomen. Kunnen "
            "jullie nakijken of ontvangen flowspec-regels daadwerkelijk "
            "toegepast worden richting deze sessie?",
        )
    else:
        cl.add(
            "Flowspec (fallback, echte host)", Status.OK,
            f"Verkeer via {host} viel wel weg - de eerdere self-reply-test was "
            "dus een vals-negatief, flowspec wordt wel degelijk gehonoreerd.",
        )


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("neighbor_ip", help="IP-adres van de neighbor (v4 of v6; zie 'bgpctl -n show summary')")
    ap.add_argument("--rtbh", action="store_true", help="alleen de RTBH-test draaien")
    ap.add_argument("--flowspec", action="store_true", help="alleen de Flowspec-test draaien")
    args = ap.parse_args()
    if not args.rtbh and not args.flowspec:
        args.rtbh = args.flowspec = True

    try:
        family = ipaddress.ip_address(args.neighbor_ip).version
    except ValueError:
        print(f"'{args.neighbor_ip}' is geen geldig IP-adres.", file=sys.stderr)
        sys.exit(2)

    cl = Checklist(f"RTBH/Flowspec-preflight - neighbor {args.neighbor_ip} (IPv{family})")

    # Bewuste keuze: RTBH/flowspec-testannouncements gaan generiek naar alle
    # neighbors, geen per-neighbor export-scoping. De test zelf blijft geldig
    # omdat de conclusie steunt op het self-reply-mechanisme van de geteste
    # neighbor (en de fallback met een echte host erachter) - niet op de
    # globale zichtbaarheid van de canary. Zie de opdrachtomschrijving voor de
    # onderbouwing.

    if not preflight_openbgpd(cl):
        sys.exit(1)
    if not preflight_session(cl, args.neighbor_ip):
        sys.exit(1)

    canary = CANARY_V4 if family == 4 else CANARY_V6
    if not preflight_canary(cl, canary, family):
        sys.exit(1)

    try:
        if args.rtbh:
            rtbh_test(cl, args.neighbor_ip, canary, family)
        if args.flowspec:
            flowspec_test(cl, args.neighbor_ip, canary, family)
    except KeyboardInterrupt:
        print("Onderbroken - cleanup is al via try/finally in de tests zelf geregeld.")
        sys.exit(130)
    except BgpdGenError as e:
        print(f"Fout: {e}", file=sys.stderr)
        sys.exit(2)

    sys.exit(1 if cl.failed else 0)


if __name__ == "__main__":
    main()
