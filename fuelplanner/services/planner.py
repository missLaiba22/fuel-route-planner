"""Ties the pieces together: locations -> route -> nearby stations -> fuel plan.

Returns a plain dict ready to be serialised as the API response.
"""

import time

from django.conf import settings

from .geocoding import resolve_location
from .optimizer import plan_fuel_stops
from .routing import get_route
from .stations import find_stations_along_route, resample_route

# Spacing of the route line returned to clients. The full OSRM geometry can be
# tens of thousands of points; one point every mile is plenty to draw a map.
RESPONSE_GEOMETRY_SPACING_MILES = 1.0


def plan_trip(start_text, finish_text):
    """Plan a trip. May raise LocationError, RoutingError or NoFeasiblePlanError."""
    started = time.perf_counter()

    start = resolve_location(start_text)
    finish = resolve_location(finish_text)
    route, api_calls = get_route(start, finish)
    stations = find_stations_along_route(route)
    plan = plan_fuel_stops(
        stations,
        route.distance_miles,
        tank_range_miles=settings.VEHICLE_RANGE_MILES,
        mpg=settings.VEHICLE_MPG,
        stop_penalty=settings.FUEL_STOP_PENALTY_DOLLARS,
    )

    stops = [_stop_dict(number, stop) for number, stop in enumerate(plan.stops, start=1)]
    line = _route_line(route)

    return {
        'start': _location_dict(start),
        'finish': _location_dict(finish),
        'distance_miles': round(route.distance_miles, 1),
        'duration_hours': round(route.duration_hours, 1),
        'vehicle': {
            'range_miles': settings.VEHICLE_RANGE_MILES,
            'miles_per_gallon': settings.VEHICLE_MPG,
            'starts_with_full_tank': True,
        },
        'fuel_stops': stops,
        'total_gallons': round(plan.total_gallons, 2),
        'total_fuel_cost': round(plan.total_cost, 2),
        'geojson': _feature_collection(start, finish, line, stops),
        'meta': {
            'stations_considered': len(stations),
            'routing_api_calls': api_calls,
            'compute_time_ms': round((time.perf_counter() - started) * 1000),
        },
    }


def _location_dict(location):
    return {'label': location.label, 'latitude': location.latitude,
            'longitude': location.longitude}


def _stop_dict(number, stop):
    s = stop.station
    return {
        'stop_number': number,
        'station_id': s.opis_id,
        'name': s.name,
        'address': s.address,
        'city': s.city,
        'state': s.state,
        'latitude': s.latitude,
        'longitude': s.longitude,
        'mile_marker': round(s.mile_marker, 1),
        'detour_miles': round(s.detour_miles, 1),
        'price_per_gallon': round(s.price, 3),
        'gallons': round(stop.gallons, 2),
        'cost': round(stop.cost, 2),
    }


def _route_line(route):
    lat, lon, _ = resample_route(route.coordinates, spacing=RESPONSE_GEOMETRY_SPACING_MILES)
    return [[round(float(x), 5), round(float(y), 5)] for x, y in zip(lon, lat)]


def _point(lon, lat, properties):
    return {'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': properties}


def _feature_collection(start, finish, line, stops):
    """Route + markers as GeoJSON (drop it into geojson.io to view)."""
    features = [
        {'type': 'Feature',
         'geometry': {'type': 'LineString', 'coordinates': line},
         'properties': {'kind': 'route'}},
        _point(start.longitude, start.latitude, {'kind': 'start', 'label': start.label}),
        _point(finish.longitude, finish.latitude, {'kind': 'finish', 'label': finish.label}),
    ]
    for stop in stops:
        features.append(_point(stop['longitude'], stop['latitude'], {
            'kind': 'fuel_stop',
            'stop_number': stop['stop_number'],
            'name': stop['name'],
            'price_per_gallon': stop['price_per_gallon'],
            'gallons': stop['gallons'],
            'cost': stop['cost'],
        }))
    return {'type': 'FeatureCollection', 'features': features}
