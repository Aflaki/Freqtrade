import os
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.pop("DATABASE_URL", None)
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
