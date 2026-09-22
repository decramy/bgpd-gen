"""Generieke, NetBox-onafhankelijke hulpfuncties: subprocess-aanroepen met
een nette foutmelding i.p.v. traceback, en atomair wegschrijven van
gegenereerde bestanden. Gedeeld door alle bgpd-gen-scripts."""
from __future__ import annotations

import subprocess
from pathlib import Path

from lib.errors import BgpdGenError


def run(cmd, **kwargs):
    """subprocess.run met check=True, maar met een nette BgpdGenError i.p.v. traceback."""
    try:
        return subprocess.run(cmd, check=True, **kwargs)
    except FileNotFoundError as e:
        raise BgpdGenError(f"commando niet gevonden: {cmd[0]} ({e})") from e
    except subprocess.CalledProcessError as e:
        detail = ""
        if e.stderr:
            detail = e.stderr if isinstance(e.stderr, str) else e.stderr.decode(errors="replace")
        raise BgpdGenError(f"{' '.join(cmd)} gaf exitcode {e.returncode}: {detail.strip()}") from e


def atomic_write(path: Path, content: str) -> None:
    """Schrijft eerst naar <path>.new en hernoemt pas bij succes, zodat een
    mislukte generatie nooit een kapot/leeg bestand achterlaat."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(content)
    tmp.replace(path)
