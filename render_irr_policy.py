#!/usr/bin/env python3
"""
render_irr_policy.py: print het RPSL import/export-beleid zoals dat in het
eigen aut-num-object (RIPE-database) zou moeten staan, afgeleid uit dezelfde
NetBox-data als de bgpd-config zelf (lib/sessions.py - dezelfde
sessions-lijst als generate.py gebruikt voor bgpd.conf).

Beleid per relationship, gededupliceerd per remote-AS (bv. twee
route-servers op dezelfde IX delen vaak hetzelfde AS-nummer en leveren dus
één regel):
  - Transit / Route Server -> accept ANY (je accepteert feitelijk de volle
    tabel van deze peers).
  - Bilateral Peering met irr_as_set -> accept <as-set-naam> (zonder de
    bron-prefix, bv. 'RIPE::', die bgpq4 nodig heeft maar RPSL niet).
  - Bilateral Peering zonder irr_as_set -> accept AS<remote-asn> (enige
    zinnige ondergrens zonder geregistreerde as-set).
Announce-kant is voor iedereen gelijk: alleen het eigen AS-nummer, want de
globale allow-filter in bgpd.conf maakt daarin geen onderscheid per
relationship.

Print alleen - schrijft niets naar de IRR weg (dat vereist je eigen
maintainer-credentials bij de betreffende routing registry, die dit script
niet beheert); kopieer de output handmatig naar het update-formulier van
je IRR-database (bv. RIPE, ARIN, RADb).
"""
from __future__ import annotations

import argparse

from lib.cli import run_main, setup_logging
from lib.netbox import load_token
from lib.router import get_router, get_scope
from lib.sessions import build_sessions


def cmd_render_irr_policy(args: argparse.Namespace) -> None:
    token = load_token()
    router = get_router(token)
    local_asn = router["asn"]["asn"]
    scope = get_scope(token)
    sessions = build_sessions(token, scope["id"])

    per_as: dict[int, dict] = {}
    for s in sessions:
        entry = per_as.setdefault(s["peer_asn"], {"relationship": s["relationship"], "irr_as_set": s["irr_as_set"]})
        if s["relationship"] in ("Transit", "Route Server"):
            entry["relationship"] = s["relationship"]

    print(f"% RPSL import/export-beleid voor AS{local_asn}, afgeleid uit NetBox.")
    print("% Alleen ter controle/kopiëren - niet automatisch naar RIPE geschreven.")
    for remote_asn in sorted(per_as):
        entry = per_as[remote_asn]
        if entry["relationship"] in ("Transit", "Route Server"):
            accept = "ANY"
        elif entry["irr_as_set"]:
            accept = entry["irr_as_set"].rsplit(":", 1)[-1]
        else:
            accept = f"AS{remote_asn}"
        print(f"import:         from AS{remote_asn} accept {accept}")
        print(f"export:         to AS{remote_asn} announce AS{local_asn}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="render_irr_policy.py",
        description="Print RPSL import/export-beleid voor het eigen aut-num (RIPE) - alleen printen, niet wegschrijven.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug-logging")
    args = parser.parse_args()
    args.log = setup_logging(args.verbose, "render-irr-policy")
    run_main(lambda: cmd_render_irr_policy(args), args.log)


if __name__ == "__main__":
    main()
