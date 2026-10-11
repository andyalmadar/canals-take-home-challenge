from __future__ import annotations

import pytest

from app.integrations.geocoding import Address, MockGeocoder, address_cache_key
from app.services.cards import luhn_valid
from app.services.geo import Coordinates, haversine_km


@pytest.mark.parametrize(
    ("number", "valid"),
    [
        ("4242424242424242", True),
        ("4000000000000002", True),
        ("4000000000000119", True),
        ("4000000000009995", True),
        ("4242424242424241", False),
        ("0000000000000000", True),
    ],
)
def test_luhn(number: str, valid: bool) -> None:
    assert luhn_valid(number) is valid


def test_haversine_known_distance() -> None:
    sf = Coordinates(37.7749, -122.4194)
    la = Coordinates(34.0522, -118.2437)
    assert haversine_km(sf, la) == pytest.approx(559, abs=2)
    assert haversine_km(sf, sf) == 0


async def test_mock_geocoder_is_deterministic_and_case_insensitive() -> None:
    geocoder = MockGeocoder()
    a = Address("1 Main St", None, "Chicago", "IL", "60601", "US")
    b = Address("1   Main St", None, "CHICAGO", "IL", "60601", "us")
    assert await geocoder.geocode(a) == Coordinates(41.8781, -87.6298)
    assert await geocoder.geocode(a) == await geocoder.geocode(b)
    assert address_cache_key(a) == address_cache_key(b)

    unknown = Address("Somewhere 1", None, "Nowhereville", None, "00000", "ZZ")
    first = await geocoder.geocode(unknown)
    assert first == await geocoder.geocode(unknown)
    assert -90 <= first.latitude <= 90 and -180 <= first.longitude <= 180
