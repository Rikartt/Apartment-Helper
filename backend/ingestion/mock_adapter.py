import json
from pathlib import Path
from .base import BaseAdapter

SAMPLE_FILE = Path(__file__).parent.parent.parent / "sample_ads.json"


class MockAdapter(BaseAdapter):
    def fetch(self) -> list[dict]:
        if not SAMPLE_FILE.exists():
            return []
        ads = json.loads(SAMPLE_FILE.read_text())
        for ad in ads:
            ad.setdefault("url", "")
        return ads
