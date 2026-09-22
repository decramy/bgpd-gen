"""Gedeelde CLI-bootstrap: dezelfde logformaat/tijdzone-keuze en dezelfde
nette afhandeling van BgpdGenError/Ctrl-C voor elk van de losse
bgpd-gen-scripts, zodat gedrag (en het exitcode-contract: 1 bij een
BgpdGenError, 130 bij Ctrl-C) niet per script opnieuw uitgevonden hoeft te
worden."""
from __future__ import annotations

import logging
import sys
import time

from lib.errors import BgpdGenError


def setup_logging(verbose: bool, name: str) -> logging.Logger:
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)sZ %(levelname)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger(name)


def run_main(func, log: logging.Logger) -> None:
    """Roept func() aan en zet BgpdGenError/Ctrl-C om in de gebruikelijke
    nette afsluiting (foutmelding zonder traceback, exitcode 1 resp. 130)."""
    try:
        func()
    except BgpdGenError as e:
        log.error("FOUT: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        log.error("Afgebroken.")
        sys.exit(130)
