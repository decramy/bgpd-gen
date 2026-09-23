"""Dunne wrapper om 'bgpctl' (OpenBGPD's CLI) - gedeeld door
verify_protection.py en activate.py, zodat er niet twee losse manieren zijn
om bgpctl aan te roepen en de foutafhandeling overal hetzelfde is."""
from __future__ import annotations

import json
import shutil
import subprocess

from lib.errors import BgpdGenError

# Bare 'bgpctl' via PATH werkt interactief (root/decramy shell), maar niet
# vanuit cron: die start met een minimale PATH zonder /usr/sbin, en faalt
# dan met FileNotFoundError vóórdat bgpctl zelf ooit draait - precies wat
# rtbh-survey-cron elk uur deed sinds de installatie (900 regels tracebacks
# in rtbh-survey.log, geen enkele geslaagde run, dus ook nooit een
# notify-mail). shutil.which respecteert een eventueel afwijkende PATH,
# met /usr/sbin/bgpctl (het standaard Debian-package-pad) als fallback voor
# precies die minimale cron-omgeving.
BGPCTL = shutil.which("bgpctl") or "/usr/sbin/bgpctl"


def bgpctl_raw(*args: str, json_out: bool = False) -> subprocess.CompletedProcess:
    """Roept bgpctl aan en geeft het ruwe CompletedProcess terug (geen
    check=True) - voor aanroepers die zelf op de returncode willen reageren
    i.p.v. een exceptie te krijgen (bv. activate.py's gezondheidschecks, die
    een falende bgpctl-aanroep als 'geen sessies bekend' willen behandelen,
    niet als harde fout)."""
    cmd = [BGPCTL] + (["-j"] if json_out else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def bgpctl(*args: str, json_out: bool = False) -> str:
    """Zoals bgpctl_raw, maar faalt hard (BgpdGenError) bij een niet-nul
    returncode - voor aanroepers (verify_protection.py) waar een mislukte
    bgpctl-aanroep altijd een fout is, nooit een legitieme lege staat."""
    r = bgpctl_raw(*args, json_out=json_out)
    if r.returncode != 0:
        raise BgpdGenError(f"bgpctl {' '.join(args)} faalde: {r.stderr.strip()}")
    return r.stdout


def bgpctl_json(*args: str):
    return json.loads(bgpctl(*args, json_out=True))
