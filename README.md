# Fuel Route Planner API

A Django API that takes a start and finish location in the USA and returns:

- the driving route (GeoJSON + an interactive map page),
- cost-effective fuel stops along it, for a vehicle with a **500-mile range**,
- the gallons bought and the **total fuel cost** at **10 MPG**.

It makes **at most one external API call per request** (routing via OSRM), and
**zero** for a repeated trip.

```
GET /api/route/?start=New York, NY&finish=Los Angeles, CA
```

| Trip | Distance | Fuel stops | Fuel cost | Response time |
|---|---|---|---|---|
| New York, NY → Los Angeles, CA | 2,801 mi | 6 | $702.73 | ~2.2 s first call (≈1.7 s is OSRM) / ~0.12 s cached |
| Chicago, IL → Denver, CO | 1,001 mi | 2 | $141.25 | ~1.2 s first call / <0.1 s cached |
| Dallas, TX → Houston, TX | 270 mi | 0 | $0.00 | fits in one (starting) tank |

---

## Quick start

Requires Python 3.12+.

```bash
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
python manage.py migrate
python manage.py load_stations   # one-time: loads cities + fuel stations (~3 s)
python manage.py runserver
```

Then open:

- **Map:** http://127.0.0.1:8000/map/?start=New+York,+NY&finish=Los+Angeles,+CA
- **API:** http://127.0.0.1:8000/api/route/?start=Chicago,+IL&finish=Denver,+CO

A Postman collection is included: [`postman_collection.json`](postman_collection.json).

> On Windows, use `127.0.0.1` rather than `localhost`. Resolving `localhost`
> adds ~0.25 s per request (IPv6 fallback), which is not the API's time.

Run the tests (no network needed, because OSRM is mocked):

```bash
python manage.py test fuelplanner
```

---

## API

### `GET /api/route/?start=...&finish=...`
### `POST /api/route/` with `{"start": "...", "finish": "..."}`

Locations can be `"City, ST"`, `"City, State Name"` (case-insensitive) or `"lat,lon"`.

**Response (abridged)**

```json
{
  "start":  {"label": "New York, NY", "latitude": 40.74838, "longitude": -73.996705},
  "finish": {"label": "Los Angeles, CA", "latitude": 33.973093, "longitude": -118.247896},
  "distance_miles": 2800.6,
  "duration_hours": 49.9,
  "vehicle": {"range_miles": 500.0, "miles_per_gallon": 10.0, "starts_with_full_tank": true},
  "fuel_stops": [
    {
      "stop_number": 1,
      "station_id": 72445,
      "name": "SHEETZ #639",
      "address": "I-80 Exit 223",
      "city": "Youngstown", "state": "OH",
      "latitude": 41.0986, "longitude": -80.6474,
      "mile_marker": 391.6,
      "detour_miles": 3.9,
      "price_per_gallon": 3.059,
      "gallons": 35.4,
      "cost": 108.29
    }
  ],
  "total_gallons": 230.1,
  "total_fuel_cost": 702.73,
  "geojson": {"type": "FeatureCollection", "features": ["route line, start, finish, one point per stop"]},
  "map_url": "http://127.0.0.1:8000/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA",
  "meta": {"stations_considered": 451, "routing_api_calls": 1, "compute_time_ms": 1840}
}
```

- `geojson` can be pasted into [geojson.io](https://geojson.io) to view the route.
- `map_url` opens the interactive Leaflet map for the same trip.
- `meta.routing_api_calls` is `1` on a new trip and `0` when the route is cached.

**Errors**

| Status | When | Example |
|---|---|---|
| 400 | Missing or unrecognised location, or outside the contiguous USA | `{"error": "Could not find city \"Gotham City, NY\"."}` |
| 422 | No feasible plan (a gap between stations exceeds the range) | `{"error": "No fuel station within 500 miles of mile 400; ..."}` |
| 502 | The routing service failed or timed out | `{"error": "Routing service unavailable: ConnectTimeout"}` |

---

## How it works

```
"New York, NY" ──► geocode (local DB) ──► route (OSRM, 1 call, cached)
                                               │
                    stations near route ◄──────┘   numpy, in memory
                            │
                    fuel plan (dynamic programming) ──► JSON + GeoJSON
```

### 1. One-time data load: `manage.py load_stations`
The provided price list has no coordinates, only city and state. Geocoding
~7,500 stations through a free API would be slow and rate limited, so instead
each station is matched to its city in a free, offline
[US cities dataset](https://github.com/kelvins/US-Cities-Database)
(`data/us_cities.csv`). All 7,531 US rows are matched. Six small places missing
from the dataset are added by hand.

The same table geocodes the user's start and finish, so geocoding costs no API
calls.

### 2. Route: [`services/routing.py`](fuelplanner/services/routing.py)
One call to the public [OSRM](https://project-osrm.org/) server (free, no API
key) returns the distance, duration and full road geometry. Routes are cached
for 24 h, so a repeated trip makes no external call.

### 3. Stations near the route: [`services/stations.py`](fuelplanner/services/stations.py)
- The road geometry (up to ~35k points) is resampled to a point every mile,
  giving each point a **mile marker**.
- Stations outside the route's bounding box are discarded.
- A coarse pass (a marker every 25 miles) discards distant stations. It cannot
  drop a valid station: anything within *D* miles of the road is within
  *D* + 12.5 miles of a coarse marker.
- A fine pass gives each remaining station its nearest mile marker and its
  distance from the road. Stations within 10 miles are kept.

Station coordinates are kept in memory as numpy arrays, so this takes about
55 ms for a coast-to-coast route.

### 4. Choosing fuel stops: [`services/optimizer.py`](fuelplanner/services/optimizer.py)
The strictly cheapest plan (the classic "gas station problem") tends to make
many tiny stops to save a few cents. Coast to coast it made **17 stops**, some
of only 1.4 gallons. That is not how a driver refuels.

So the planner minimises **fuel cost + $10 per stop**. The $10 is a
planning-only stand-in for the time and hassle of a stop, and is **never
included in the reported cost**. Coast to coast this gives **6 stops for $7
more** (about 1%).

It is solved exactly with **dynamic programming over (station, reach)**, where
*reach* = mile marker + miles of fuel in the tank. Driving does not change
reach, so each station is one vectorised numpy update. This takes about 20 ms
for a coast-to-coast route. With the penalty set to 0, the tests check it
against a brute-force solver on hundreds of random trips.

---

## Assumptions and design decisions

| Decision | Why |
|---|---|
| The vehicle **starts with a full tank** (500 miles). Total cost = fuel bought along the route. | A common trucking assumption. Trips under 500 miles need no stops and cost $0. |
| **$10 planning penalty per stop** (configurable, 0 = strictly cheapest) | Avoids unrealistic 1-gallon stops for a ~1% cost difference. |
| Station coordinates are **city-level** | Avoids geocoding ~7,500 addresses through a rate-limited API. Accurate enough to place a station along an interstate route. |
| A station counts as on the route if it is within **10 miles** (straight line) | Absorbs the gap between city-centre coordinates and the actual exit. Configurable. |
| Duplicate station IDs with different prices: **keep the lowest price** | The price list repeats some stations under different rack (supply) IDs. |
| **Canadian stations are excluded** | The assignment is USA only. |
| Contiguous USA only | Alaska and Hawaii cannot be reached by road from the lower 48. |
| Fuel is planned in whole miles | May buy up to ~0.1 gal more than strictly needed (cents). |

## Configuration

All settings have sensible defaults and can be overridden through environment
variables or a `.env` file (see [`.env.example`](.env.example)).

| Variable | Default | Meaning |
|---|---|---|
| `VEHICLE_RANGE_MILES` | `500` | Distance on a full tank |
| `VEHICLE_MPG` | `10` | Fuel economy |
| `FUEL_STOP_PENALTY_DOLLARS` | `10` | Planning cost per stop (0 = strictly cheapest) |
| `MAX_STATION_DETOUR_MILES` | `10` | How far from the road a station may be |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | Routing server |
| `OSRM_TIMEOUT_SECONDS` | `15` | Routing request timeout |
| `ROUTE_CACHE_SECONDS` | `86400` | How long routes are cached |

## Project layout

```
config/                         Django settings and URLs
fuelplanner/
  models.py                     City, FuelStation
  management/commands/
    load_stations.py            one-time CSV import + coordinate matching
  services/
    geocoding.py                "City, ST" -> coordinates (local, no API)
    routing.py                  OSRM call + cache
    stations.py                 stations near the route + mile markers
    optimizer.py                fuel stop planning (dynamic programming)
    planner.py                  ties it together into the response
  views.py, serializers.py      REST API (Django REST Framework)
  templates/fuelplanner/map.html  Leaflet map page
  tests.py                      28 tests (OSRM mocked)
data/us_cities.csv              offline US city coordinates
fuel-prices-for-be-assessment.csv  provided price list
```

## Production notes

- The public OSRM server is a free demo service with no uptime guarantee. For
  production, point `OSRM_BASE_URL` at a self-hosted OSRM instance.
- The route cache is in-process (`LocMemCache`). With several workers, use Redis
  so all workers share it.
- After re-running `load_stations`, restart the server: each process keeps the
  station coordinates in memory.

Built with Django 6.1, Django REST Framework, numpy, OSRM, Leaflet and OpenStreetMap.
