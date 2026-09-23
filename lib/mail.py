"""Simpele e-mail-verzending via de lokale sendmail-compatible MTA (op
buggy: nullmailer, relayt naar mail.xentower.nl) - geen SMTP-library of
extra dependency nodig, standaard unix-conventie (`sendmail -t`, adressen
uit de headers zelf, geen aparte envelope-argumenten)."""
from __future__ import annotations

import shutil
import subprocess

from lib.errors import BgpdGenError

MAIL_FROM = "bgpd-gen@buggy.xentower.nl"

# Zelfde PATH-probleem als lib/bgpctl.py: cron draait met een minimale PATH
# (/bin:/usr/bin, geen /usr/sbin) - een kale "sendmail" zou daar met
# FileNotFoundError falen, vóórdat sendmail zelf ooit draait.
SENDMAIL = shutil.which("sendmail") or "/usr/sbin/sendmail"


def send_mail(to: str, subject: str, body: str, from_addr: str = MAIL_FROM) -> None:
    message = f"From: {from_addr}\nTo: {to}\nSubject: {subject}\n\n{body}\n"
    try:
        r = subprocess.run([SENDMAIL, "-t"], input=message, text=True, capture_output=True)
    except OSError as e:
        raise BgpdGenError(f"{SENDMAIL} -t kon niet gestart worden: {e}") from e
    if r.returncode != 0:
        raise BgpdGenError(f"{SENDMAIL} -t faalde: {r.stderr.strip()}")
