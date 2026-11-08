"""Bootstrap settings. Everything else is edited from the admin panel and stored in the database.

Only two secrets are needed and they are generated automatically on first start into
data/secrets.json (back this file up!). They can also be given as env vars.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_secrets() -> tuple[str, str]:
    path = DATA_DIR / "secrets.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    changed = False
    if not os.getenv("SECRET_KEY") and "SECRET_KEY" not in data:
        data["SECRET_KEY"] = secrets.token_urlsafe(48)
        changed = True
    if not os.getenv("MASTER_KEY") and "MASTER_KEY" not in data:
        data["MASTER_KEY"] = Fernet.generate_key().decode()
        changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2))
        os.chmod(path, 0o600)
    return os.getenv("SECRET_KEY") or data["SECRET_KEY"], os.getenv("MASTER_KEY") or data["MASTER_KEY"]


SECRET_KEY, MASTER_KEY = _load_secrets()
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'relay.db').as_posix()}")
