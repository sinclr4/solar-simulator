from __future__ import annotations

import os
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

IN_AZURE = bool(os.environ.get("WEBSITE_SITE_NAME"))
LOCAL_KEY_FILE = Path.home() / ".solarsim" / "credentials.key"


def _key() -> bytes:
    key = os.environ.get("CREDENTIALS_KEY")
    if key:
        return key.encode()
    if IN_AZURE:
        raise ValueError("The server has no CREDENTIALS_KEY setting, so Octopus details cannot be stored")
    # Local development only: keep a generated key outside the repository
    if not LOCAL_KEY_FILE.exists():
        LOCAL_KEY_FILE.parent.mkdir(mode=0o700, exist_ok=True)
        LOCAL_KEY_FILE.write_bytes(Fernet.generate_key())
        LOCAL_KEY_FILE.chmod(0o600)
    return LOCAL_KEY_FILE.read_bytes().strip()


def encrypt(value: str) -> str:
    return Fernet(_key()).encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return Fernet(_key()).decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("Saved credentials can no longer be read (wrong CREDENTIALS_KEY?) - please enter them again") from None


API_KEY_RE = re.compile(r"^sk_(live|test)_[A-Za-z0-9]{10,100}$")
ACCOUNT_RE = re.compile(r"^A-[A-Z0-9]{4,12}$")


def clean_api_key(value: str) -> str:
    value = (value or "").strip()
    if not API_KEY_RE.match(value):
        raise ValueError("That does not look like an Octopus API key (it starts with sk_live_)")
    return value


def clean_account(value: str) -> str:
    value = (value or "").strip().upper()
    if not ACCOUNT_RE.match(value):
        raise ValueError("Account number should look like A-1234ABCD")
    return value
