from abc import ABC, abstractmethod


class BaseAdapter(ABC):
    """Yield raw ad dicts ready for upsert into the listings table."""

    # Set by fetch(): True when the run read only a window of the source (page/ad caps,
    # tiling depth cap, partial pagination, robots-limited paging). Gone detection
    # must not conclude "unseen == removed" after a truncated run.
    truncated: bool = False
    truncated_reason: str = ""

    @abstractmethod
    def fetch(self) -> list[dict]:
        """Return a list of raw ad dicts with at minimum:
        source, external_id, title, address, lat, lng,
        rent_chf, available_from, raw_text, url
        """
