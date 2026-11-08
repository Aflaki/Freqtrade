import os
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

_tmp = tempfile.mkdtemp()
os.environ.update(
    SECRET_KEY="test-secret-key-" + "x" * 20,
    MASTER_KEY=Fernet.generate_key().decode(),
    WEBHOOK_SECRET="webhook-secret-123456",
    DATABASE_URL=f"sqlite:///{_tmp}/test.db",
    TELEGRAM_BOT_TOKEN="",
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
