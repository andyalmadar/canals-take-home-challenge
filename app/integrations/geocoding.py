"""Address -> coordinates.

`Geocoder` is the port. `MockGeocoder` stands in for a third-party API (Google,
Mapbox, Nominatim...). `CachedGeocoder` wraps any geocoder with a Postgres-backed
cache, because real geocoding is slow, rate-limited and billed per call, and the
same shipping address shows up again and again.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.dialects.postgresql import insert

from app.db.models import GeocodeCacheEntry
from app.db.session import SessionFactory
from app.logging import get_logger
from app.services.geo import Coordinates

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Address:
    line1: str
    line2: str | None
    city: str
    region: str | None
    postal_code: str
    country: str


class GeocodingError(Exception):
    """The geocoding provider failed or could not resolve the address."""


class Geocoder(Protocol):
    async def geocode(self, address: Address) -> Coordinates: ...


_WHITESPACE = re.compile(r"\s+")


def normalize_address(address: Address) -> str:
    parts = [
        address.line1,
        address.line2 or "",
        address.city,
        address.region or "",
        address.postal_code,
        address.country,
    ]
    joined = "|".join(_WHITESPACE.sub(" ", p.strip().lower()) for p in parts)
    return joined


def address_cache_key(address: Address) -> str:
    return hashlib.sha256(normalize_address(address).encode()).hexdigest()


# A handful of real city centroids so demo requests land somewhere sensible.
_KNOWN_CITIES: dict[tuple[str, str], Coordinates] = {
    ("san francisco", "US"): Coordinates(37.7749, -122.4194),
    ("los angeles", "US"): Coordinates(34.0522, -118.2437),
    ("san diego", "US"): Coordinates(32.7157, -117.1611),
    ("seattle", "US"): Coordinates(47.6062, -122.3321),
    ("portland", "US"): Coordinates(45.5152, -122.6784),
    ("las vegas", "US"): Coordinates(36.1699, -115.1398),
    ("phoenix", "US"): Coordinates(33.4484, -112.0740),
    ("denver", "US"): Coordinates(39.7392, -104.9903),
    ("dallas", "US"): Coordinates(32.7767, -96.7970),
    ("austin", "US"): Coordinates(30.2672, -97.7431),
    ("houston", "US"): Coordinates(29.7604, -95.3698),
    ("chicago", "US"): Coordinates(41.8781, -87.6298),
    ("minneapolis", "US"): Coordinates(44.9778, -93.2650),
    ("atlanta", "US"): Coordinates(33.7490, -84.3880),
    ("miami", "US"): Coordinates(25.7617, -80.1918),
    ("new york", "US"): Coordinates(40.7128, -74.0060),
    ("newark", "US"): Coordinates(40.7357, -74.1724),
    ("boston", "US"): Coordinates(42.3601, -71.0589),
    ("philadelphia", "US"): Coordinates(39.9526, -75.1652),
    ("washington", "US"): Coordinates(38.9072, -77.0369),
    ("toronto", "CA"): Coordinates(43.6532, -79.3832),
    ("vancouver", "CA"): Coordinates(49.2827, -123.1207),
    ("mexico city", "MX"): Coordinates(19.4326, -99.1332),
    ("london", "GB"): Coordinates(51.5074, -0.1278),
    ("paris", "FR"): Coordinates(48.8566, 2.3522),
    ("berlin", "DE"): Coordinates(52.5200, 13.4050),
    ("madrid", "ES"): Coordinates(40.4168, -3.7038),
    ("buenos aires", "AR"): Coordinates(-34.6037, -58.3816),
    ("sao paulo", "BR"): Coordinates(-23.5505, -46.6333),
}


class MockGeocoder:
    """Deterministic stand-in for a geocoding API.

    Known (city, country) pairs resolve to their real centroid. Anything else
    hashes to a stable pseudo-location, so repeated requests for the same
    unknown address always agree with each other.
    """

    async def geocode(self, address: Address) -> Coordinates:
        city = _WHITESPACE.sub(" ", address.city.strip().lower())
        city = city.replace("ã", "a")
        known = _KNOWN_CITIES.get((city, address.country.upper()))
        if known is not None:
            return known
        digest = hashlib.sha256(normalize_address(address).encode()).digest()
        lat_unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        lon_unit = int.from_bytes(digest[4:8], "big") / 0xFFFFFFFF
        return Coordinates(latitude=-60 + lat_unit * 130, longitude=-180 + lon_unit * 360)


class CachedGeocoder:
    def __init__(self, inner: Geocoder, session_factory: SessionFactory) -> None:
        self._inner = inner
        self._sessions = session_factory

    async def geocode(self, address: Address) -> Coordinates:
        key = address_cache_key(address)
        async with self._sessions() as session:
            cached = await session.get(GeocodeCacheEntry, key)
        if cached is not None:
            return Coordinates(cached.latitude, cached.longitude)

        coords = await self._inner.geocode(address)
        log.debug("geocode_cache_miss", address_key=key)
        async with self._sessions() as session, session.begin():
            stmt = (
                insert(GeocodeCacheEntry)
                .values(
                    address_key=key,
                    normalized_address=normalize_address(address),
                    latitude=coords.latitude,
                    longitude=coords.longitude,
                )
                .on_conflict_do_nothing(index_elements=[GeocodeCacheEntry.address_key])
            )
            await session.execute(stmt)
        return coords
