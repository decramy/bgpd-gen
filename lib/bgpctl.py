"""Dunne wrapper om 'bgpctl' (OpenBGPD's CLI) - gedeeld door
verify_protection.py en activate.py, zodat er niet twee losse manieren zijn
om bgpctl aan te roepen en de foutafhandeling overal hetzelfde is."""
from __future__ import annotations

import json
import subprocess

from lib.errors import BgpdGenError


def bgpctl_raw(*args: str, json_out: bool = False) -> subprocess.CompletedProcess:
    """Roept bgpctl aan en geeft het ruwe CompletedProcess terug (geen
    check=True) - voor aanroepers die zelf op de returncode willen reageren
    i.p.v. een exceptie te krijgen (bv. activate.py's gezondheidschecks, die
    een falende bgpctl-aanroep als 'geen sessies bekend' willen behandelen,
    niet als harde fout)."""
    cmd = ["bgpctl"] + (["-j"] if json_out else []) + list(args)
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
