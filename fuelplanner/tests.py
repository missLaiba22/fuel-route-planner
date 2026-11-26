import random
from dataclasses import dataclass
from unittest import mock

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from fuelplanner.models import City, FuelStation
from fuelplanner.services.geocoding import Location, LocationError, resolve_location
from fuelplanner.services.optimizer import NoFeasiblePlanError, plan_fuel_stops
from fuelplanner.services.routing import Route, RoutingError, get_route
from fuelplanner.services.stations import (
    find_stations_along_route, get_station_index, haversine_miles, resample_route,
)


class ResolveLocationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        City.objects.create(name='Chicago', name_key='chicago', state='IL',
                            latitude=41.8781, longitude=-87.6298)
        City.objects.create(name='Honolulu', name_key='honolulu', state='HI',
                            latitude=21.3069, longitude=-157.8583)

    def test_city_and_state_code(self):
        loc = resolve_location('Chicago, IL')
        self.assertEqual(loc.label, 'Chicago, IL')
        self.assertAlmostEqual(loc.latitude, 41.8781)

    def test_case_insensitive_full_state_name(self):
        self.assertEqual(resolve_location('  chicago ,  illinois ').label, 'Chicago, IL')

    def test_lat_lon(self):
        loc = resolve_location('39.7392,-104.9903')
        self.assertAlmostEqual(loc.longitude, -104.9903)

    def test_errors(self):
        for bad in ['', 'Chicago', 'Toronto, ON', 'Gotham, IL', '51.5,-0.12', 'Honolulu, HI']:
            with self.subTest(bad=bad), self.assertRaises(LocationError):
                resolve_location(bad)


def fake_response(payload, status=200):
    response = mock.Mock(status_code=status)
    response.json.return_value = payload
    return response


class GetRouteTests(TestCase):
    start = Location('A', 41.0, -87.0)
    finish = Location('B', 39.0, -105.0)

    def setUp(self):
        cache.clear()

    @mock.patch('fuelplanner.services.routing.requests.get')
    def test_parses_route_and_caches(self, get):
        get.return_value = fake_response({
            'code': 'Ok',
            'routes': [{'distance': 1609344, 'duration': 36000,
                        'geometry': {'coordinates': [[-87, 41], [-105, 39]]}}],
        })
        route, calls = get_route(self.start, self.finish)
        self.assertEqual(calls, 1)
        self.assertAlmostEqual(route.distance_miles, 1000)
        self.assertAlmostEqual(route.duration_hours, 10)
        self.assertEqual(route.coordinates, [[-87, 41], [-105, 39]])

        _, calls = get_route(self.start, self.finish)
        self.assertEqual(calls, 0)
        self.assertEqual(get.call_count, 1)  # second call served from cache

    @mock.patch('fuelplanner.services.routing.requests.get')
    def test_no_route(self, get):
        get.return_value = fake_response({'code': 'NoRoute', 'message': 'Impossible route'}, 400)
        with self.assertRaisesMessage(RoutingError, 'Impossible route'):
            get_route(self.start, self.finish)

    @mock.patch('fuelplanner.services.routing.requests.get',
                side_effect=requests.Timeout())
    def test_network_error(self, get):
        with self.assertRaisesMessage(RoutingError, 'unavailable'):
            get_route(self.start, self.finish)


def make_station(opis_id, lat, lon, price='3.00'):
    return FuelStation.objects.create(
        opis_id=opis_id, name=f'S{opis_id}', address='', city='X', state='KS',
        price=price, latitude=lat, longitude=lon)


class StationsAlongRouteTests(TestCase):
    # Straight east-west route along latitude 40, from lon -100 to lon -90.
    route_coords = [[-100.0 + i * 0.1, 40.0] for i in range(101)]

    def setUp(self):
        get_station_index.cache_clear()
        self.length = float(haversine_miles(40, -100, 40, -90))
        self.route = Route(distance_miles=self.length, duration_hours=8,
                           coordinates=self.route_coords)

    def tearDown(self):
        get_station_index.cache_clear()

    def test_resample_route_spacing_and_length(self):
        lat, lon, miles = resample_route(self.route_coords)
        # Following a line of latitude is slightly longer than the great circle.
        self.assertAlmostEqual(miles[-1], self.length, delta=0.5)
        self.assertTrue((abs(lat - 40) < 1e-9).all())
        self.assertAlmostEqual(miles[1] - miles[0], 1.0)

    def test_resample_scales_to_router_distance(self):
        _, _, miles = resample_route(self.route_coords, total_miles=1000)
        self.assertAlmostEqual(miles[-1], 1000)

    def test_finds_nearby_stations_with_mile_markers(self):
        on_route = make_station(1, 40.0, -95.0)
        five_north = make_station(2, 40.0 + 5 / 69, -92.0)
        make_station(3, 40.0 + 30 / 69, -95.0)   # 30 miles off route
        make_station(4, 40.0, -85.0)             # past the destination

        found = find_stations_along_route(self.route, max_detour_miles=10)

        self.assertEqual([s.opis_id for s in found], [on_route.opis_id, five_north.opis_id])
        halfway = self.length / 2
        self.assertAlmostEqual(found[0].mile_marker, halfway, delta=1)
        # Markers are 1 mile apart, so precision is +/- half a mile.
        self.assertAlmostEqual(found[0].detour_miles, 0, delta=0.5)
        self.assertAlmostEqual(found[1].detour_miles, 5, delta=0.5)

    def test_detour_threshold_respected(self):
        make_station(1, 40.0 + 8 / 69, -95.0)    # 8 miles off route
        self.assertEqual(len(find_stations_along_route(self.route, max_detour_miles=5)), 0)
        get_station_index.cache_clear()
        self.assertEqual(len(find_stations_along_route(self.route, max_detour_miles=10)), 1)

    def test_no_stations(self):
        self.assertEqual(find_stations_along_route(self.route), [])


@dataclass(frozen=True)
class S:
    mile_marker: float
    price: float


def brute_force_cost(stations, total, tank, start_fuel):
    """Exact optimum by dynamic programming over integer fuel levels (tests only)."""
    stations = sorted(stations, key=lambda s: s.mile_marker)
    points = [s.mile_marker for s in stations] + [total]
    INF = float('inf')
    # best[f] = min cost to be at the current point with f miles of fuel.
    if points[0] > start_fuel:
        return None
    best = [INF] * (tank + 1)
    best[start_fuel - points[0]] = 0.0
    for i, st in enumerate(stations):
        leg = points[i + 1] - points[i]
        after = [INF] * (tank + 1)
        for f, cost in enumerate(best):
            if cost == INF:
                continue
            for target in range(max(f, leg), tank + 1):
                c = cost + (target - f) * st.price
                if c < after[target - leg]:
                    after[target - leg] = c
        best = after
    result = min(best)
    return None if result == INF else result


class PlanFuelStopsTests(SimpleTestCase):
    def test_short_trip_needs_no_stops(self):
        plan = plan_fuel_stops([S(100, 3.0)], total_miles=400)
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_cost, 0)

    def test_buys_just_enough_to_reach_cheaper_station(self):
        # Worked example: B is cheap, D is cheaper still.
        stations = [S(120, 3.50), S(300, 3.10), S(450, 3.80), S(620, 2.90), S(880, 3.40)]
        plan = plan_fuel_stops(stations, total_miles=1000, mpg=10, tank_range_miles=500)
        bought = [(s.mile_marker, round(s.gallons, 2)) for s in plan.stops]
        # At B buy 120 mi to reach D; at D buy 380 mi to finish.
        self.assertEqual(bought, [(300, 12.0), (620, 38.0)])
        self.assertAlmostEqual(plan.total_cost, 12 * 3.10 + 38 * 2.90)
        self.assertAlmostEqual(plan.total_gallons, 50)

    def test_fills_up_when_nothing_cheaper_ahead(self):
        stations = [S(400, 2.50), S(800, 4.00), S(850, 3.00)]
        plan = plan_fuel_stops(stations, total_miles=1200, tank_range_miles=500, mpg=10)
        # Fill up at mile 400, then buy just enough at mile 850.
        self.assertEqual([(s.mile_marker, round(s.gallons, 2)) for s in plan.stops],
                         [(400, 40.0), (850, 30.0)])

    def test_start_with_empty_tank(self):
        plan = plan_fuel_stops([S(0, 3.0), S(300, 2.0)], total_miles=600, start_fuel_miles=0)
        self.assertEqual([(s.mile_marker, round(s.gallons, 2)) for s in plan.stops],
                         [(0, 30.0), (300, 30.0)])
        self.assertAlmostEqual(plan.total_cost, 30 * 3 + 30 * 2)

    def test_stop_penalty_skips_tiny_savings(self):
        # An extra stop at mile 100 saves only 10 cents.
        stations = [S(100, 2.99), S(450, 3.00)]
        cheapest = plan_fuel_stops(stations, total_miles=900, stop_penalty=0)
        self.assertEqual([s.mile_marker for s in cheapest.stops], [100, 450])
        self.assertAlmostEqual(cheapest.total_cost, 10 * 2.99 + 30 * 3.00)

        practical = plan_fuel_stops(stations, total_miles=900, stop_penalty=10)
        self.assertEqual([s.mile_marker for s in practical.stops], [450])
        # Penalty is for planning only: reported cost is fuel money.
        self.assertAlmostEqual(practical.total_cost, 40 * 3.00)
        self.assertAlmostEqual(practical.total_gallons, 40)

    def test_penalty_never_makes_plan_infeasible(self):
        stations = [S(400, 3.0), S(800, 3.5), S(1200, 3.0)]
        plan = plan_fuel_stops(stations, total_miles=1500, stop_penalty=1000)
        self.assertEqual(len(plan.stops), 3)  # every stop is required

    def test_gap_too_large(self):
        with self.assertRaisesMessage(NoFeasiblePlanError, 'mile 1100'):
            plan_fuel_stops([S(400, 3.0), S(1100, 3.0)], total_miles=1500)

    def test_cannot_reach_first_station(self):
        with self.assertRaises(NoFeasiblePlanError):
            plan_fuel_stops([S(50, 3.0)], total_miles=600, start_fuel_miles=10)

    def test_ignores_stations_off_the_trip(self):
        plan = plan_fuel_stops([S(-5, 1.0), S(250, 3.0), S(700, 0.5)], total_miles=600)
        self.assertEqual([s.mile_marker for s in plan.stops], [250])

    def test_matches_brute_force_optimum(self):
        rng = random.Random(42)
        checked = 0
        for _ in range(400):
            tank = rng.randint(20, 60)
            total = rng.randint(10, 200)
            start = rng.randint(0, tank)
            stations = [S(rng.randint(0, total), round(rng.uniform(2.5, 4.5), 2))
                        for _ in range(rng.randint(0, 12))]
            expected = brute_force_cost(stations, total, tank, start)
            try:
                plan = plan_fuel_stops(stations, total, tank_range_miles=tank, mpg=1,
                                       start_fuel_miles=start)
            except NoFeasiblePlanError:
                self.assertIsNone(expected, 'greedy gave up on a feasible trip')
                continue
            self.assertIsNotNone(expected, 'greedy found a plan for an infeasible trip')
            self.assertAlmostEqual(plan.total_cost, expected, places=6)
            checked += 1
        self.assertGreater(checked, 100)


class RoutePlanApiTests(TestCase):
    """End-to-end API tests with OSRM mocked out."""

    @classmethod
    def setUpTestData(cls):
        City.objects.create(name='Westville', name_key='westville', state='KS',
                            latitude=40.0, longitude=-100.0)
        City.objects.create(name='Eastville', name_key='eastville', state='MO',
                            latitude=40.0, longitude=-85.0)
        make_station(1, 40.0, -95.0, price='3.50')   # ~265 mi
        make_station(2, 40.0, -92.0, price='2.90')   # ~424 mi
        make_station(3, 40.0, -89.0, price='3.10')   # ~583 mi

    def setUp(self):
        cache.clear()
        get_station_index.cache_clear()
        coords = [[-100.0 + i * 0.1, 40.0] for i in range(151)]
        miles = float(sum(haversine_miles(a[1], a[0], b[1], b[0])
                          for a, b in zip(coords, coords[1:])))
        patcher = mock.patch('fuelplanner.services.routing.requests.get', return_value=fake_response({
            'code': 'Ok',
            'routes': [{'distance': miles * 1609.344, 'duration': 36000,
                        'geometry': {'coordinates': coords}}],
        }))
        self.osrm = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        get_station_index.cache_clear()

    def test_get_plans_route(self):
        res = self.client.get('/api/route/', {'start': 'Westville, KS', 'finish': 'Eastville, MO'})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertAlmostEqual(data['distance_miles'], 794, delta=2)
        # Full tank reaches mile 500: cheapest is to fill at the $2.90 station.
        self.assertEqual([s['station_id'] for s in data['fuel_stops']], [2])
        stop = data['fuel_stops'][0]
        self.assertAlmostEqual(stop['cost'], stop['gallons'] * 2.90, places=1)
        self.assertEqual(data['total_fuel_cost'], stop['cost'])
        self.assertEqual(data['meta']['routing_api_calls'], 1)
        kinds = [f['properties']['kind'] for f in data['geojson']['features']]
        self.assertEqual(kinds, ['route', 'start', 'finish', 'fuel_stop'])
        self.assertIn('/map/?start=Westville', data['map_url'])

    def test_post_json_and_cache(self):
        body = {'start': 'Westville, KS', 'finish': 'Eastville, MO'}
        self.client.post('/api/route/', body, content_type='application/json')
        res = self.client.post('/api/route/', body, content_type='application/json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['meta']['routing_api_calls'], 0)
        self.assertEqual(self.osrm.call_count, 1)

    def test_validation_errors(self):
        self.assertEqual(self.client.get('/api/route/').status_code, 400)
        res = self.client.get('/api/route/', {'start': 'Nowhere, KS', 'finish': 'Eastville, MO'})
        self.assertEqual(res.status_code, 400)
        self.assertIn('Could not find', res.json()['error'])
        self.osrm.assert_not_called()

    def test_routing_failure_is_502(self):
        self.osrm.side_effect = requests.ConnectionError()
        res = self.client.get('/api/route/', {'start': 'Westville, KS', 'finish': 'Eastville, MO'})
        self.assertEqual(res.status_code, 502)

    def test_unreachable_is_422(self):
        FuelStation.objects.all().delete()
        get_station_index.cache_clear()
        res = self.client.get('/api/route/', {'start': 'Westville, KS', 'finish': 'Eastville, MO'})
        self.assertEqual(res.status_code, 422)
        self.assertIn('first fuel station', res.json()['error'])

    def test_map_page(self):
        res = self.client.get('/map/')
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, '/api/route/')
