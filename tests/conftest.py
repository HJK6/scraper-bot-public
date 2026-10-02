"""Isolate test data before server imports."""
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="scraper-bot-test-"))
os.environ.setdefault("SCRAPERBOT_DATA_DIR", str(_TEST_DATA_DIR))
