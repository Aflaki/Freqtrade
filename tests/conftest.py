import os
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.pop("DATABASE_URL", None)
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
os.environ["RELAY_STREAMS"] = "0"  # no live exchange WebSockets in tests (test_streams drives them with fakes)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# auth: no 3-second form timer and no Have-I-Been-Pwned network calls in tests
from app.auth import captcha as _captcha, passwords as _passwords  # noqa: E402

_captcha.MIN_SECONDS = 0


async def _never_breached(password: str) -> bool:
    return False


_passwords.breached = _never_breached
