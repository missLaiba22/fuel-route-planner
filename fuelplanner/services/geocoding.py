"""Turn user input ("Chicago, IL") into coordinates using the local City table.

No external API is called, so geocoding the start and finish costs nothing
and leaves the single external call for routing.

Accepted formats:
    "Chicago, IL"            city + state code
    "Chicago, Illinois"      city + full state name
    "41.8781,-87.6298"       raw "lat,lon"
"""

import re
from dataclasses import dataclass

from fuelplanner.models import City

US_STATES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA',
    'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE', 'district of columbia': 'DC',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL',
    'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA',
    'maine': 'ME', 'maryland': 'MD', 'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN',
    'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR',
    'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC', 'south dakota': 'SD',
    'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA',
    'washington': 'WA', 'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
}
STATE_CODES = set(US_STATES.values())

# Rough bounding box of the contiguous USA (routes must be drivable).
US_LAT_RANGE = (24.0, 50.0)
US_LON_RANGE = (-125.0, -66.0)

COORD_RE = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$')


class LocationError(ValueError):
    """The input could not be resolved to a location in the USA."""


@dataclass(frozen=True)
class Location:
    label: str
    latitude: float
    longitude: float


def _in_usa(lat, lon):
    return US_LAT_RANGE[0] <= lat <= US_LAT_RANGE[1] and US_LON_RANGE[0] <= lon <= US_LON_RANGE[1]


def resolve_location(text):
    """Resolve free text to a Location, or raise LocationError."""
    if not text or not text.strip():
        raise LocationError('Location is required.')
    text = text.strip()

    match = COORD_RE.match(text)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        if not _in_usa(lat, lon):
            raise LocationError(f'"{text}" is outside the contiguous USA.')
        return Location(label=f'{lat:.5f},{lon:.5f}', latitude=lat, longitude=lon)

    if ',' not in text:
        raise LocationError(f'"{text}" should look like "City, ST" (e.g. "Chicago, IL").')

    city_part, state_part = (p.strip() for p in text.rsplit(',', 1))
    state = state_part.upper() if state_part.upper() in STATE_CODES else US_STATES.get(state_part.lower())
    if state is None:
        raise LocationError(f'"{state_part}" is not a US state.')

    city_key = ' '.join(city_part.lower().split())
    city = City.objects.filter(name_key=city_key, state=state).first()
    if city is None:
        raise LocationError(f'Could not find city "{city_part}, {state}".')
    if not _in_usa(city.latitude, city.longitude):
        raise LocationError(f'"{city}" is outside the contiguous USA (not reachable by road).')

    return Location(label=str(city), latitude=city.latitude, longitude=city.longitude)
