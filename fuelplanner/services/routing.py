"""Driving route via the free OSRM API (the only external call; cached)."""

from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

METERS_PER_MILE = 1609.344


class RoutingError(Exception):
    """The routing service failed or found no route."""


@dataclass(frozen=True)
class Route:
    distance_miles: float
    duration_hours: float
    # [lon, lat] pairs along the road.
    coordinates: list


def get_route(start, finish):
    """Return (route, api_calls): 1 if OSRM was called, 0 if cached."""
    cache_key = 'route:{:.5f},{:.5f}:{:.5f},{:.5f}'.format(
        start.latitude, start.longitude, finish.latitude, finish.longitude)
    route = cache.get(cache_key)
    if route is not None:
        return route, 0
    route = _fetch_route(start, finish)
    cache.set(cache_key, route, settings.ROUTE_CACHE_SECONDS)
    return route, 1


def _fetch_route(start, finish):
    url = '{base}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}'.format(
        base=settings.OSRM_BASE_URL.rstrip('/'),
        lon1=start.longitude, lat1=start.latitude,
        lon2=finish.longitude, lat2=finish.latitude,
    )
    params = {'overview': 'full', 'geometries': 'geojson', 'steps': 'false'}

    try:
        response = requests.get(url, params=params, timeout=settings.OSRM_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RoutingError(f'Routing service unavailable: {exc.__class__.__name__}') from exc

    try:
        data = response.json()
    except ValueError:
        raise RoutingError(f'Routing service returned an invalid response (HTTP {response.status_code}).')

    if data.get('code') != 'Ok' or not data.get('routes'):
        message = data.get('message') or data.get('code') or f'HTTP {response.status_code}'
        raise RoutingError(f'No route found: {message}')

    best = data['routes'][0]
    return Route(
        distance_miles=best['distance'] / METERS_PER_MILE,
        duration_hours=best['duration'] / 3600,
        coordinates=best['geometry']['coordinates'],
    )
