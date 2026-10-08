import os
import sys
from pathlib import Path

# handlers.py lives in src/ and the workers in src/amazon_uk/ — put src/ on
# sys.path (as handlers.py itself does in Lambda) so tests import them directly.
_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

# Config must be env-backed; give tests a minimal valid env before any
# module import touches it.
os.environ.setdefault("LWA_CLIENT_ID", "test")
os.environ.setdefault("LWA_CLIENT_SECRET", "test")
os.environ.setdefault("LWA_REFRESH_TOKEN", "test")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("HANA_HOST", "hana")
os.environ.setdefault("HANA_USER", "test")
os.environ.setdefault("HANA_PASSWORD", "test")
os.environ.setdefault("HANA_SCHEMA", "TEST")
