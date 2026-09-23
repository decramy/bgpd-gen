#!/usr/bin/env python3
"""rtbh_survey.py: test alle deelnemers van een IX-fabric (via de/het
route-server(s) van die fabric) op RTBH-gedrag, en schrijft het resultaat
weg als platte tekst naar rtbh-surveys/<fabric-slug>-v<4|6>.txt
(RTBH_SURVEYS_DIR, lib/config.py) - één regel per ooit geziene
deelnemer-ASN, met verdict OK (RTBH aantoonbaar gehonoreerd), FAIL (niet
gehonoreerd) of SKIP (nooit positief bevestigd, want al onbereikbaar op de
baseline). Bewust geen NetBox-object hiervoor (in tegenstelling tot de
transit-AS-lijst) - dit is een tussentijds testresultaat, geen beheerde
configuratie-invoer.

Draait standaard beide address families (v4 én v6 zijn onafhankelijke
testen - RTBH-gedrag over v4 zegt niets gegarandeerd over v6, dus ook
aparte resultaatbestanden per family, nooit één lijst voor beide
hergebruikt). Een ASN waarvan het verdict tussen v4 en v6 verschilt (bv.
OK op v4, FAIL op v6) wordt aan het eind met een [WARN]-regel gemeld -
dat is zelf geen fout, maar wel iets om te weten vóór je 'm zonder verder
kijken vertrouwt.

Bewust GEEN selectieve export van eigen prefixes op basis van deze data
(dat bestond eerder wel - IXP Manager-communityschema <rs-asn>:0:0/
<rs-asn>:1:<asn> - maar is losgehaald: het maakte de test zelf nodeloos
complex, zie hieronder). Dit script test en rapporteert alleen; wat
generate.py/peer-group.conf.j2 met de uitkomst doen is aan een latere,
losse beslissing.

Het probleem dat dit zichtbaar maakt: een RTBH-afspraak heb je met je
transit (die honoreert 'm dus), maar niet met losse IXP-deelnemers. Tijdens
een echte aanval via de IXP blijven de meeste deelnemers gewoon verkeer
doorsturen, RTBH of niet - de enige "oplossing" zonder dit inzicht zou zijn
de hele IXP te verlaten. Deze test bootst een echte RTBH-activatie zo
letterlijk mogelijk na: de canary gaat naar ALLE sessies (RS, transit,
bilateraal - dezelfde 'allow to any prefix X or-longer'-regel als altijd,
geen uitzonderingen meer), precies zoals een daadwerkelijke incident-
respons ook zou gaan.

Meting: TTL=1 ICMP-pings vanaf de canary naar elke deelnemers' peering-LAN-
IP, individueel en parallel (geen fping - die kan de uitgaande TTL niet
zetten). Buggy heeft zelf een interface op de IXP-peering-LAN, dus een
daadwerkelijke buur is altijd precies 1 hop weg; TTL=1 dwingt af dat het
verzoek uitsluitend over die rechtstreekse hop kan, en vangt zo een
onverwacht indirect pad in buggy's eigen routeringstabel af vóórdat het
verzoek ooit vertrekt.

Mechanisme, zelfde principe als verify_protection.py maar in bulk en via
de route-server i.p.v. bilateraal: (1) nulmeting - TTL=1-sweep van alle
deelnemers, via de altijd al aanwezige aggregate-prefix (geen aparte
activatie nodig: de canary zit daar al in besloten). (2) canary met
BLACKHOLE-community adverteren - naar alles en iedereen. (3) RTBH_WAIT
wachten. (4) daadwerkelijke test - alleen de nulmeting-responders opnieuw
pingen. (5) canary weer volledig intrekken (try/finally, ongeacht fouten).
Wie tijdens fase 4 wegviel, wordt na RECOVERY_WAIT nogmaals gepingd
(eindmeting, via de weer-aanwezige aggregate): pas als die ook weer
reageert - dus aantoonbaar wegviel ZOLANG de blackhole actief was en
terugkwam ZODRA 'm ingetrokken werd - telt het als OK. Wegvallen zonder
aantoonbaar herstel wordt SKIP (kan een toevallige, ongerelateerde storing
zijn geweest, geen bevestigd RTBH-gedrag) i.p.v. voorbarig als OK geteld.

Raakt NetBox nooit (schrijfrechten) - alleen bgpd.conf's netwerk-RIB via
bgpctl (tijdelijk, tijdens de test zelf), en het eigen resultaatbestand.

Kan vanuit cron draaien (netbox-rtbh-survey-cron, elk uur, vóór
generate.py's eigen cron) - bewuste keuze van de gebruiker, ondanks dat dit
per definitie intrusief is tegen externe netwerken (elk uur opnieuw een
korte blackhole richting elke deelnemer die 'm honoreert). Vergelijkt bij
elke run het nieuwe verdict per ASN met het vorige (vóór het resultaat-
bestand overschreven wordt) en mailt NOTIFY_EMAIL (config.py) zodra een
deelnemer van gedrag wisselt - geen NOTIFY_EMAIL ingevuld betekent alleen
een logregel, geen harde fout.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import subprocess
import time
from datetime import datetime, timezone

from lib.bgpctl import bgpctl, bgpctl_json
from lib.cli import run_main, setup_logging
from lib.config import CANARY_V4, CANARY_V6, NOTIFY_EMAIL, RTBH_SURVEYS_DIR
from lib.errors import BgpdGenError
from lib.mail import send_mail
from lib.netbox import load_token
from lib.router import get_scope
from lib.sessions import build_sessions

RTBH_COMMUNITY = "65535:666"
RTBH_WAIT = 30  # seconden na het adverteren van de canary, vóór de daadwerkelijke test - ruimer dan verify_protection.py's 5s: hier gaat het via een route-server naar veel deelnemers tegelijk, niet één bilaterale sessie.
RECOVERY_WAIT = 30  # seconden na het intrekken van de canary, vóór de eindmeting (herstel-bevestiging)

PING_COUNT = 2  # pogingen per host, tegen incidentele ICMP-ruis
PING_TIMEOUT = 1  # seconden wachten op antwoord per poging (ping -W)
PING_TTL = 1  # buggy zit zelf op de IXP-peering-LAN - een daadwerkelijke buur is altijd 1 hop weg
PING_WORKERS = 50  # losse ping's per host i.p.v. één fping-sweep (die kan de uitgaande TTL niet zetten) - parallel om de doorlooptijd beperkt te houden


def fabric_slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def find_route_server_ips(token: str, fabric: str, family: int) -> list[str]:
    scope = get_scope(token)
    sessions = build_sessions(token, scope["id"])
    want_v6 = family == 6
    return sorted({
        s["remote_ip"] for s in sessions
        if s["relationship"] == "Route Server"
        and s.get("peering_network") and s["peering_network"]["fabric"] == fabric
        and (":" in s["remote_ip"]) == want_v6
    })


def enumerate_participants(rs_ips: list[str]) -> dict[int, str]:
    """{deelnemer-ASN: hun peering-LAN-IP (true_nexthop)}, verzameld over
    alle opgegeven route-server-sessies van de fabric (bv. rs1+rs2) - een
    deelnemer die niet op elke RS zit, wordt toch meegenomen zodra hij op
    minstens één van de twee voorkomt."""
    result: dict[int, str] = {}
    for rs_ip in rs_ips:
        data = bgpctl_json("show", "rib", "neighbor", rs_ip)
        for entry in data.get("rib", []):
            aspath = entry.get("aspath") or ""
            if not aspath:
                continue
            asn = int(aspath.split()[0])
            result.setdefault(asn, entry["true_nexthop"])
    return result


def _ping_one(ip: str, canary: str, family: int) -> bool:
    cmd = [
        "ping", "-4" if family == 4 else "-6",
        "-c", str(PING_COUNT), "-W", str(PING_TIMEOUT), "-t", str(PING_TTL),
        "-I", canary, ip,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0


def ping_sweep(ips: list[str], canary: str, family: int) -> set[str]:
    """Geeft de subset van ips terug die reageerde op een TTL=1-ping vanaf
    canary - losse ping's per host, parallel (PING_WORKERS tegelijk) omdat
    fping de uitgaande TTL niet kan zetten. TTL=1 forceert dat het verzoek
    alleen over de rechtstreekse, direct-verbonden peering-LAN-hop kan."""
    if not ips:
        return set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=PING_WORKERS) as pool:
        results = pool.map(lambda ip: (ip, _ping_one(ip, canary, family)), ips)
    return {ip for ip, ok in results if ok}


def survey_file_path(fabric: str, family: int):
    return RTBH_SURVEYS_DIR / f"{fabric_slug(fabric)}-v{family}.txt"


def write_survey_results(fabric: str, family: int, results: dict[int, str], log) -> None:
    """results: {asn: 'OK'|'FAIL'|'SKIP'}. Schrijft naar
    rtbh-surveys/<fabric-slug>-v<family>.txt, één regel '<asn> <verdict>'
    per ASN, oplopend gesorteerd - simpel, leesbaar, diff-baar, geen
    NetBox-object ervoor nodig (dit is een tussentijds testresultaat, geen
    beheerde configuratie-invoer zoals de transit-AS-lijst dat wel is).
    Overschrijft het hele bestand elke run (geen incrementele update nodig
    zoals bij NetBox-schrijven, dus ook geen 'oude regel laten staan'-
    risico)."""
    path = survey_file_path(fabric, family)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    lines = [
        f"# rtbh-survey: {fabric} (route-server, IPv{family}), gegenereerd {now} door rtbh_survey.py",
        "# format: <asn> <OK|FAIL|SKIP>",
    ]
    lines += [f"{asn} {verdict}" for asn, verdict in sorted(results.items())]
    path.write_text("\n".join(lines) + "\n")
    log.info("weggeschreven: %s (%d ASNs)", path, len(results))


def read_previous_verdicts(fabric: str, family: int) -> dict[int, str]:
    """Het vorige survey-resultaat (indien aanwezig), vóór het door deze
    run overschreven wordt - nodig om wijzigingen t.o.v. de vorige keer te
    kunnen melden. Geen eerder bestand (allereerste run) -> lege dict, geen
    fout: dan is er simpelweg niets om mee te vergelijken."""
    path = survey_file_path(fabric, family)
    if not path.exists():
        return {}
    verdicts: dict[int, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        asn_str, verdict = line.split()
        verdicts[int(asn_str)] = verdict
    return verdicts


def diff_verdicts(old: dict[int, str], new: dict[int, str]) -> list[tuple[int, str, str]]:
    """ASNs die in zowel de vorige als de nieuwe run voorkwamen, met een
    ander verdict - een nieuw verschenen of verdwenen deelnemer is geen
    gedragswijziging (er is dan geen 'vorige staat' om mee te vergelijken)
    en wordt hier bewust niet in meegenomen."""
    changed = []
    for asn in sorted(set(old) & set(new)):
        if old[asn] != new[asn]:
            changed.append((asn, old[asn], new[asn]))
    return changed


def notify_changes(fabric: str, changes_by_family: dict[int, list[tuple[int, str, str]]], log) -> None:
    total = sum(len(c) for c in changes_by_family.values())
    if not total:
        return
    if not NOTIFY_EMAIL:
        log.warning("%d ASN(s) van RTBH-gedrag gewisseld, maar NOTIFY_EMAIL staat niet in config.py - geen mail verstuurd.", total)
        return
    subject = f"RTBH-survey {fabric}: {total} peer(s) van gedrag gewisseld"
    lines = [f"RTBH-survey van {fabric} vond {total} gewijzigd(e) verdict(en) t.o.v. de vorige run:", ""]
    for family in sorted(changes_by_family):
        changes = changes_by_family[family]
        if not changes:
            continue
        lines.append(f"IPv{family}:")
        for asn, old_v, new_v in changes:
            lines.append(f"  AS{asn}: {old_v} -> {new_v}")
        lines.append("")
    lines.append("(automatisch gegenereerd door rtbh_survey.py)")
    try:
        send_mail(NOTIFY_EMAIL, subject, "\n".join(lines))
        log.info("mail verstuurd naar %s (%d wijziging(en))", NOTIFY_EMAIL, total)
    except BgpdGenError as e:
        log.error("mail versturen mislukt: %s", e)


def survey_one_family(token: str, fabric: str, family: int, log) -> dict[int, str]:
    """Draait de volledige sweep voor één address family en geeft
    {asn: 'OK'|'FAIL'|'SKIP'} terug - schrijft zelf nog niets weg, dat
    doet de aanroeper (zodat cmd_survey eerst v4+v6 kan vergelijken vóór
    beide bestanden geschreven worden)."""
    canary = CANARY_V4 if family == 4 else CANARY_V6
    if not canary:
        raise BgpdGenError(f"CANARY_V{family} staat niet ingevuld in config.py")

    rs_ips = find_route_server_ips(token, fabric, family)
    if not rs_ips:
        raise BgpdGenError(
            f"geen Route Server-sessie (IPv{family}) gevonden voor fabric '{fabric}' in NetBox - "
            f"typefout in de fabricnaam, of deze fabric heeft geen route-server-sessies van dit address family"
        )
    log.info("route-server(s) voor '%s' (IPv%d): %s", fabric, family, ", ".join(rs_ips))

    participants = enumerate_participants(rs_ips)
    log.info("%d deelnemers gevonden (aggregatie over %d route-server-sessie(s))",
              len(participants), len(rs_ips))
    ip_to_asn = {ip: asn for asn, ip in participants.items()}
    all_ips = sorted(ip_to_asn)

    log.info("1. nulmeting (TTL=1, vóór RTBH)...")
    baseline = ping_sweep(all_ips, canary, family)
    log.info("nulmeting: %d/%d deelnemers reageren", len(baseline), len(all_ips))

    canary_pfx = f"{canary}/{32 if family == 4 else 128}"
    log.info("2. RTBH-community activeren op %s (naar alle sessies - RS, transit, bilateraal)...", canary_pfx)
    bgpctl("network", "add", canary_pfx, "community", RTBH_COMMUNITY)
    try:
        log.info("3. %ds wachten...", RTBH_WAIT)
        time.sleep(RTBH_WAIT)
        log.info("4. daadwerkelijke test (TTL=1, alleen nulmeting-responders)...")
        during = ping_sweep(sorted(baseline), canary, family)
    finally:
        log.info("5. canary volledig intrekken...")
        bgpctl("network", "delete", canary_pfx)

    dropped = baseline - during
    recovered: set[str] = set()
    if dropped:
        log.info("%d deelnemer(s) stopten met reageren tijdens RTBH - %ds wachten vóór eindmeting...",
                  len(dropped), RECOVERY_WAIT)
        time.sleep(RECOVERY_WAIT)
        log.info("6. eindmeting (TTL=1, herstel-bevestiging van de weggevallen deelnemers)...")
        recovered = ping_sweep(sorted(dropped), canary, family)
    else:
        log.info("niemand viel weg tijdens RTBH - geen eindmeting nodig.")

    results: dict[int, str] = {}
    for ip, asn in ip_to_asn.items():
        if ip not in baseline:
            results[asn] = "SKIP"  # nooit bereikbaar op de nulmeting - niet te testen
        elif ip not in dropped:
            results[asn] = "FAIL"  # bleef reageren tijdens RTBH - niet gehonoreerd
        elif ip in recovered:
            results[asn] = "OK"  # viel weg tijdens RTBH, kwam terug ná intrekken - bevestigd
        else:
            results[asn] = "SKIP"  # viel weg, maar geen bevestigd herstel - mogelijk ongerelateerde storing

    ok = sum(1 for v in results.values() if v == "OK")
    fail = sum(1 for v in results.values() if v == "FAIL")
    skip = sum(1 for v in results.values() if v == "SKIP")
    log.info("IPv%d-resultaat: %d OK, %d FAIL, %d SKIP (van %d)", family, ok, fail, skip, len(results))
    return results


def warn_on_family_mismatch(fabric: str, results_by_family: dict[int, dict[int, str]], log) -> None:
    """Meldt (WARN, niet ERROR) elke ASN waarvan het verdict tussen de
    getestte families verschilt - RTBH-gedrag over v4 garandeert niets
    over v6 (en omgekeerd), dus zo'n verschil is geen bug, maar wel iets
    om te weten vóór je de resultaten zonder verder kijken vertrouwt."""
    families = sorted(results_by_family)
    if len(families) < 2:
        return
    f1, f2 = families[0], families[1]
    r1, r2 = results_by_family[f1], results_by_family[f2]
    common = sorted(set(r1) & set(r2))
    mismatches = [asn for asn in common if r1[asn] != r2[asn]]
    if not mismatches:
        log.info("geen verschil tussen IPv%d- en IPv%d-verdict voor de %d ASNs die in beide voorkomen.",
                  f1, f2, len(common))
        return
    log.warning("%d ASN(s) hebben een verschillend verdict tussen IPv%d en IPv%d (fabric '%s'):",
                len(mismatches), f1, f2, fabric)
    for asn in mismatches:
        log.warning("  AS%d: IPv%d=%s, IPv%d=%s", asn, f1, r1[asn], f2, r2[asn])


def cmd_survey(args: argparse.Namespace) -> None:
    log = args.log
    token = load_token()
    families = [args.family] if args.family else [4, 6]

    results_by_family: dict[int, dict[int, str]] = {}
    for family in families:
        results_by_family[family] = survey_one_family(token, args.fabric, family, log)

    if len(results_by_family) > 1:
        warn_on_family_mismatch(args.fabric, results_by_family, log)

    # Vorige verdicts ophalen vóór de bestanden overschreven worden -
    # anders is er niets meer om de wijziging mee te vergelijken.
    changes_by_family: dict[int, list[tuple[int, str, str]]] = {}
    for family, results in results_by_family.items():
        previous = read_previous_verdicts(args.fabric, family)
        changes_by_family[family] = diff_verdicts(previous, results)

    for family, results in results_by_family.items():
        write_survey_results(args.fabric, family, results, log)

    notify_changes(args.fabric, changes_by_family, log)

    log.info("Klaar. Puur informatief - beïnvloedt bgpd.conf niet (geen selectieve export meer op basis van deze data).")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="rtbh_survey.py",
        description="Test alle route-server-deelnemers van een IX-fabric op RTBH-gedrag en schrijf het resultaat weg.",
    )
    ap.add_argument("fabric", help="NetBox PeeringFabric-naam, bv. 'Frys-IX' (exacte match)")
    ap.add_argument("--family", type=int, choices=(4, 6), default=None,
                     help="beperk tot één address family (standaard: beide, v4 en v6)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug-logging")
    args = ap.parse_args()
    args.log = setup_logging(args.verbose, "rtbh-survey")
    run_main(lambda: cmd_survey(args), args.log)


if __name__ == "__main__":
    main()
