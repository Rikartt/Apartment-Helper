from pydantic import BaseModel
from typing import Optional, Any


class Listing(BaseModel):
    id: int
    source: str
    external_id: str
    title: str
    address: str
    lat: float
    lng: float
    rent_chf: int
    available_from: str
    raw_text: str
    url: str = ""
    bike_zentrum_min: Optional[int] = None
    transit_zentrum_min: Optional[int] = None
    bike_hoenggerberg_min: Optional[int] = None
    transit_hoenggerberg_min: Optional[int] = None
    seen: bool = False
    sent: bool = False
    replied: bool = False
    extracted: Optional[dict[str, Any]] = None
    fit_score: Optional[int] = None
