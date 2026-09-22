#!/usr/bin/env python3
"""
activate.py: valideert de laatst door generate.py gegenereerde output/ en
zet 'm live, met automatische rollback bij regressie. Losse, handmatige stap
- NIET door cron aangeroepen (zie generate.py/README.md voor waarom).

Bewust GEEN wijziging aan de systemd-unit (blijft Debian-standaard, verwacht
/etc/bgpd.conf): in plaats daarvan staat er een permanente, één-regelige stub
op /etc/bgpd.conf die naar de echte, door dit script beheerde config in
/etc/bgpd/ verwijst. Omdat bgpd relatieve include-paden oplost t.o.v. zijn
eigen working directory (niet t.o.v. het insluitende bestand), gebruiken de
templates zelf al absolute "/etc/bgpd/peers/..."-includes - validatie moet
daarom tegen de daadwerkelijke live locatie (ná het kopiëren), niet tegen
output/.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

from lib.bgpctl import bgpctl_raw
from lib.cli import run_main, setup_logging
from lib.config import LIVE_DIR, LIVE_ENTRYPOINT, OUTPUT_DIR
from lib.errors import BgpdGenError
from lib.util import run

# Hoe lang na het (her)starten gewacht wordt voordat 'bgpctl show summary'
# gecontroleerd wordt op Established. Alleen relevant voor sessies die
# daadwerkelijk opnieuw moeten opbouwen (nieuwe/gewijzigde neighbor - een
# 'reload' laat ongewijzigde, al lopende sessies met rust, zie README). Een
# full-table-sessie (bv. een transit-peer) kan na Established nog een tijd
# bezig zijn met het verwerken van 1M+ prefixes; te vroeg checken riskeert
# een onterechte rollback - verhoog dan met --wait.
HEALTHCHECK_DELAY = 15

LIVE_ENTRYPOINT_CONTENT = f'include "{LIVE_DIR}/bgpd.conf"\n'


def _openbgpd_is_active() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", "openbgpd"]).returncode == 0


def _openbgpd_start_or_reload() -> None:
    """Debian's openbgpd.service vereist /etc/bgpd.conf om te starten
    (ConditionPathExists) en 'reload' werkt alleen op een reeds actieve
    service - bij de allereerste activatie is de service enabled maar
    inactive, dan moet het 'start' zijn i.p.v. 'reload'."""
    run(["systemctl", "reload" if _openbgpd_is_active() else "start", "openbgpd"])


def _established_ips() -> set[str]:
    """IP-adressen van neighbors die op dit moment Established zijn, via
    'bgpctl -n show summary' (numerieke IP's i.p.v. beschrijvingen - nodig
    om betrouwbaar te matchen op adres, ongeacht 'descr'-tekst/naamgeving).
    De laatste kolom (State/PrfRcvd) is een getal zodra Established. Gebruikt
    bgpctl_raw (geen check=True): bgpd dat nog niet draait telt hier gewoon
    als 'geen sessies Established', geen harde fout."""
    summary = bgpctl_raw("-n", "show", "summary").stdout
    established = set()
    for line in summary.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        ip, state = parts[0], parts[-1]
        if re.match(r"^\d+(/\d+)?$", state):
            established.add(ip)
    return established


def _enabled_neighbor_ips() -> set[str]:
    """Neighbor-IP's in de live peers/*.conf die NIET 'down' staan."""
    enabled = set()
    for conf in (LIVE_DIR / "peers").glob("*.conf"):
        for m in re.finditer(r"^neighbor (\S+) \{(.*?)^\}", conf.read_text(), re.MULTILINE | re.DOTALL):
            ip, body = m.group(1), m.group(2)
            if not re.search(r"^\s*down\s*$", body, re.MULTILINE):
                enabled.add(ip)
    return enabled


def cmd_activate(args: argparse.Namespace) -> None:
    log = args.log
    staged_bgpd_conf = OUTPUT_DIR / "bgpd.conf"
    if not staged_bgpd_conf.exists():
        raise BgpdGenError(f"{staged_bgpd_conf} ontbreekt - draai eerst 'generate.py generate'")
    if shutil.which("bgpd") is None:
        raise BgpdGenError("bgpd niet gevonden in PATH - is openbgpd geïnstalleerd?")
    if os.geteuid() != 0:
        raise BgpdGenError("activate moet als root draaien (systemctl, /etc/bgpd.conf en /etc/bgpd/ schrijven)")

    # Vastleggen vóór er iets verandert: alleen een sessie die HIERVANDAAN
    # Established was en dat na de reload niet meer is, is een echte
    # regressie. Sessies die al niet Established waren (bv. een peer waar
    # de tegenpartij nog aan werkt) blijven buiten beschouwing, ongeacht wat
    # er in deze activatie verder wijzigt (bv. alleen de 'descr'-tekst).
    old_established = _established_ips()

    log.info("1. Stub-entrypoint %s controleren...", LIVE_ENTRYPOINT)
    LIVE_ENTRYPOINT.write_text(LIVE_ENTRYPOINT_CONTENT)

    # Alleen een echte, eerder geactiveerde bgpd.conf telt als terugrol-doel.
    # LIVE_DIR.exists() alleen is niet genoeg: die map bestaat op een verse
    # installatie al (met een lege peers/-submap, geen bgpd.conf) - dat per
    # ongeluk als "vorige goede staat" behandelen liet een mislukte eerste
    # activatie terugrollen naar een lege, niet-functionele config, die zich
    # vervolgens als "backup" bij een volgende activatie weer voortplantte.
    backup = None
    if (LIVE_DIR / "bgpd.conf").exists():
        backup = LIVE_DIR.parent / f"{LIVE_DIR.name}.backup-{time.strftime('%Y%m%d%H%M%S')}"
        log.info("2. Backup van huidige live config naar %s...", backup)
        shutil.copytree(LIVE_DIR, backup)
    else:
        log.info("2. Geen eerdere werkende %s/bgpd.conf gevonden, eerste activatie.", LIVE_DIR)

    log.info("3. Nieuwe configuratie kopiëren naar %s...", LIVE_DIR)
    if (LIVE_DIR / "peers").exists():
        shutil.rmtree(LIVE_DIR / "peers")
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(OUTPUT_DIR, LIVE_DIR, dirs_exist_ok=True)

    def _rollback_copy() -> None:
        shutil.rmtree(LIVE_DIR)
        shutil.copytree(backup, LIVE_DIR)

    log.info("4. Syntax-validatie (tegen de live locatie, %s)...", LIVE_ENTRYPOINT)
    result = subprocess.run(["bgpd", "-n", "-f", str(LIVE_ENTRYPOINT)], capture_output=True, text=True)
    if result.returncode != 0:
        if backup is not None:
            _rollback_copy()
            log.error("Configuratie is ongeldig, teruggerold naar %s (nog niet herladen).", backup)
        else:
            log.error("Configuratie is ongeldig (eerste activatie, niets om naar terug te rollen).")
        raise BgpdGenError(f"bgpd -n faalde:\n{result.stderr}")
    log.info("   OK.")

    reload_time = time.strftime("%Y-%m-%d %H:%M:%S")
    log.info("5. Activeren (%s)...", "reload" if _openbgpd_is_active() else "start")
    _openbgpd_start_or_reload()

    log.info("6. Failsafe-check (%ds wachten)...", args.wait)
    time.sleep(args.wait)
    summary = bgpctl_raw("show", "summary").stdout
    log.info("%s", summary)

    new_established = _established_ips()
    enabled_now = _enabled_neighbor_ips()
    regressed = sorted((old_established & enabled_now) - new_established)
    if regressed:
        log.error("WAARSCHUWING: sessie(s) die vóór deze activatie Established waren, zijn dat nu niet "
                   "meer (%s). Terugrollen...", ", ".join(regressed))
        journal = subprocess.run(
            ["journalctl", "-u", "openbgpd", "--no-pager", "--since", reload_time],
            capture_output=True, text=True,
        ).stdout
        log.error("openbgpd-log sinds het (her)laden (%s):\n%s", reload_time, journal)
        if backup is not None:
            _rollback_copy()
            _openbgpd_start_or_reload()
            log.error("Teruggerold naar %s.", backup)
        else:
            log.error("Geen backup beschikbaar (eerste activatie) - handmatig ingrijpen nodig.")
        sys.exit(2)

    log.info("Klaar. Config actief, geen regressie t.o.v. de vorige staat "
              "(sessies die al niet Established waren, tellen niet mee).")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="activate.py",
        description="Valideer en zet de laatst gegenereerde output/ live (handmatig, NIET door cron).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug-logging")
    parser.add_argument(
        "--wait", type=int, default=HEALTHCHECK_DELAY, metavar="SECONDEN",
        help=f"wachttijd vóór de Established-controle (standaard {HEALTHCHECK_DELAY}s). "
             f"Zet hoger bij een eerste activatie van een full-table-peer (bv. transit) - "
             f"een sessie met 1M+ prefixes kan langer dan de standaardwaarde nodig hebben om te "
             f"convergeren. Bij een routinematige reload van al langer lopende sessies is de "
             f"standaardwaarde meestal voldoende.",
    )
    args = parser.parse_args()
    args.log = setup_logging(args.verbose, "activate")
    run_main(lambda: cmd_activate(args), args.log)


if __name__ == "__main__":
    main()
