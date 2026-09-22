"""Simpele e-mail-verzending via de lokale sendmail-compatible MTA (op
buggy: nullmailer, relayt naar mail.xentower.nl) - geen SMTP-library of
extra dependency nodig, standaard unix-conventie (`sendmail -t`, adressen
uit de headers zelf, geen aparte envelope-argumenten)."""
from __future__ import annotations

import subprocess

from lib.errors import BgpdGenError

MAIL_FROM = "bgpd-gen@buggy.xentower.nl"


def send_mail(to: str, subject: str, body: str, from_addr: str = MAIL_FROM) -> None:
    message = f"From: {from_addr}\nTo: {to}\nSubject: {subject}\n\n{body}\n"
    r = subprocess.run(["sendmail", "-t"], input=message, text=True, capture_output=True)
    if r.returncode != 0:
        raise BgpdGenError(f"sendmail -t faalde: {r.stderr.strip()}")
