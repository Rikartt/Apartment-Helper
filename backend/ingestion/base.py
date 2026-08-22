from abc import ABC, abstractmethod


class BaseAdapter(ABC):
    """Yield raw ad dicts ready for upsert into the listings table."""

    @abstractmethod
    def fetch(self) -> list[dict]:
        """Return a list of raw ad dicts with at minimum:
        source, external_id, title, address, lat, lng,
        rent_chf, available_from, raw_text, url
        """
