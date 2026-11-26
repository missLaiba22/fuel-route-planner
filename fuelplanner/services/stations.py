"""Find fuel stations close to a route, and how far along the route each one is.

How it works (all vectorised with numpy, no external calls):

1. Measure the route: cumulative miles at every route vertex (haversine).
2. Resample the route to evenly spaced "mile markers" (one every SAMPLE_SPACING
   miles). Irregular OSRM geometry (tens of thousands of points) becomes a
   compact, uniform polyline.
3. Cheap pre-filter: keep stations inside the route's bounding box + buffer.
4. Coarse pass against a marker every 25 miles discards far-away stations.
5. Fine pass: for each survivor, find the nearest 1-mile marker. If it is
   within `max_detour_miles`, the station is on the route at that mile.

Station coordinates are held in memory as numpy arrays (loaded once per
process) so a request never scans the database row by row.
"""

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from django.conf import settings

from fuelplanner.models import FuelStation

EARTH_RADIUS_MILES = 3958.8
MILES_PER_DEGREE_LAT = 69.0
SAMPLE_SPACING_MILES = 1.0
COARSE_SPACING_MILES = 25.0
STATION_CHUNK = 512  # stations compared against the route per numpy batch


@dataclass(frozen=True)
class StationOnRoute:
    id: int
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    latitude: float
    longitude: float
    mile_marker: float          # miles from the start, along the route
    detour_miles: float         # straight-line distance from the route


@dataclass(frozen=True)
class StationIndex:
    ids: np.ndarray
    lat: np.ndarray
    lon: np.ndarray


@lru_cache(maxsize=1)
def get_station_index():
    """All stations' coordinates as numpy arrays (cached for the process)."""
    rows = list(FuelStation.objects.values_list('id', 'latitude', 'longitude'))
    if not rows:
        empty = np.array([], dtype=float)
        return StationIndex(np.array([], dtype=int), empty, empty)
    data = np.array(rows, dtype=float)
    return StationIndex(ids=data[:, 0].astype(int), lat=data[:, 1], lon=data[:, 2])


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles (numpy arrays or scalars)."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))


def resample_route(coordinates, total_miles=None, spacing=SAMPLE_SPACING_MILES):
    """Return (lat, lon, mile) arrays for points every `spacing` miles.

    `coordinates` are [lon, lat] pairs (GeoJSON order). If `total_miles` is
    given (the router's own distance), mile markers are scaled to match it so
    they agree with the distance reported to the user.
    """
    coords = np.asarray(coordinates, dtype=float)
    lon, lat = coords[:, 0], coords[:, 1]

    segment = haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])
    cumulative = np.concatenate(([0.0], np.cumsum(segment)))
    length = cumulative[-1]

    if length == 0:
        return lat[:1], lon[:1], np.array([0.0])

    miles = np.append(np.arange(0.0, length, spacing), length)
    sample_lat = np.interp(miles, cumulative, lat)
    sample_lon = np.interp(miles, cumulative, lon)

    if total_miles:
        miles = miles * (total_miles / length)
    return sample_lat, sample_lon, miles


def _nearest_samples(s_lat, s_lon, r_lat, r_lon):
    """For each station, index of and distance to the nearest route sample.

    Uses a local flat-earth approximation: accurate at the few-mile scale we
    care about, and only ever compared against small thresholds. Processed in
    chunks so memory stays bounded on long routes.
    """
    n = s_lat.size
    nearest_idx = np.empty(n, dtype=int)
    nearest_dist = np.empty(n, dtype=float)
    for start in range(0, n, STATION_CHUNK):
        lat = s_lat[start:start + STATION_CHUNK, None]
        lon = s_lon[start:start + STATION_CHUNK, None]
        dy = (r_lat[None, :] - lat) * MILES_PER_DEGREE_LAT
        dx = (r_lon[None, :] - lon) * (MILES_PER_DEGREE_LAT * np.cos(np.radians(lat)))
        dist_sq = dx * dx + dy * dy
        best = dist_sq.argmin(axis=1)
        nearest_idx[start:start + lat.shape[0]] = best
        nearest_dist[start:start + lat.shape[0]] = np.sqrt(dist_sq[np.arange(lat.shape[0]), best])
    return nearest_idx, nearest_dist


def find_stations_along_route(route, max_detour_miles=None):
    """Stations within `max_detour_miles` of the route, sorted by mile marker."""
    if max_detour_miles is None:
        max_detour_miles = settings.MAX_STATION_DETOUR_MILES
    index = get_station_index()
    if index.ids.size == 0:
        return []

    r_lat, r_lon, r_mile = resample_route(route.coordinates, route.distance_miles)

    # 1) Bounding-box pre-filter (cheap; discards most of the country).
    lat_pad = max_detour_miles / MILES_PER_DEGREE_LAT
    max_abs_lat = min(max(abs(r_lat.min()), abs(r_lat.max())) + lat_pad, 89.0)
    lon_pad = lat_pad / np.cos(np.radians(max_abs_lat))
    in_box = ((index.lat >= r_lat.min() - lat_pad) & (index.lat <= r_lat.max() + lat_pad)
              & (index.lon >= r_lon.min() - lon_pad) & (index.lon <= r_lon.max() + lon_pad))
    candidates = np.nonzero(in_box)[0]
    if candidates.size == 0:
        return []

    # 2) Coarse pass: compare against a marker every COARSE_SPACING miles.
    #    Every point of the route is within COARSE_SPACING/2 road-miles (and so
    #    straight-line miles) of a coarse marker, so a station within D of the
    #    route is always within D + COARSE_SPACING/2 of one: nothing is lost.
    step = max(1, int(round(COARSE_SPACING_MILES / SAMPLE_SPACING_MILES)))
    _, coarse_dist = _nearest_samples(index.lat[candidates], index.lon[candidates],
                                      r_lat[::step], r_lon[::step])
    candidates = candidates[coarse_dist <= max_detour_miles + COARSE_SPACING_MILES / 2 + 1]
    if candidates.size == 0:
        return []

    # 3) Fine pass on the survivors: nearest 1-mile marker.
    nearest_idx, nearest_dist = _nearest_samples(index.lat[candidates], index.lon[candidates],
                                                 r_lat, r_lon)

    close = nearest_dist <= max_detour_miles
    if not close.any():
        return []

    mile_by_id = {
        int(index.ids[c]): (float(r_mile[i]), float(d))
        for c, i, d in zip(candidates[close], nearest_idx[close], nearest_dist[close])
    }

    # 4) Fetch details only for the stations we kept.
    results = []
    for s in FuelStation.objects.filter(id__in=mile_by_id.keys()):
        mile, detour = mile_by_id[s.id]
        results.append(StationOnRoute(
            id=s.id, opis_id=s.opis_id, name=s.name, address=s.address,
            city=s.city, state=s.state, price=float(s.price),
            latitude=s.latitude, longitude=s.longitude,
            mile_marker=round(mile, 2), detour_miles=round(detour, 2),
        ))
    results.sort(key=lambda s: (s.mile_marker, s.price))
    return results
