"""One-time data load: US cities + fuel stations with coordinates.

    python manage.py load_stations

The fuel price CSV has no coordinates, only "City, State". Rather than
geocode ~7,500 stations through an external API (slow, rate limited), we
match each station's city against a free offline US cities dataset.
Coordinates are therefore city-level, which is accurate enough to decide
whether a station lies along a long-distance route.

The command is idempotent: it replaces existing data on every run.
"""

import csv
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuelplanner.models import City, FuelStation
from fuelplanner.services.stations import get_station_index

# Canadian provinces/territories in the price list. The assignment is USA only.
NON_US_STATES = {'AB', 'BC', 'MB', 'NB', 'NL', 'NS', 'NT', 'NU', 'ON', 'PE', 'QC', 'SK', 'YT'}

# A few small US places missing from the cities dataset.
MANUAL_COORDINATES = {
    ('port wentworth', 'GA'): (32.1491, -81.1632),
    ('university park', 'IL'): (41.4428, -87.6837),
    ('evergreen', 'AL'): (31.4335, -86.9569),
    ('henrico', 'VA'): (37.5059, -77.3325),
    ('elizabethport', 'NJ'): (40.6476, -74.1838),
    ('brookpark', 'OH'): (41.3984, -81.8046),
}


def city_key(name):
    return ' '.join(name.strip().lower().split())


class Command(BaseCommand):
    help = 'Load US cities and fuel stations (with coordinates) into the database.'

    def add_arguments(self, parser):
        parser.add_argument('--prices', default=str(settings.FUEL_PRICES_CSV),
                            help='Path to the fuel prices CSV.')
        parser.add_argument('--cities', default=str(settings.US_CITIES_CSV),
                            help='Path to the US cities CSV.')

    def handle(self, *args, **options):
        cities = self.read_cities(options['cities'])
        stations, skipped_non_us, unmatched = self.read_stations(options['prices'], cities)

        with transaction.atomic():
            City.objects.all().delete()
            FuelStation.objects.all().delete()
            City.objects.bulk_create(
                [City(name=name, name_key=key[0], state=key[1], latitude=lat, longitude=lon)
                 for key, (name, lat, lon) in cities.items()],
                batch_size=2000,
            )
            FuelStation.objects.bulk_create(stations, batch_size=2000)

        # Drop this process's in-memory station index. A running server keeps
        # its own copy, so restart it after reloading data.
        get_station_index.cache_clear()

        self.stdout.write(self.style.SUCCESS(
            f'Loaded {len(cities)} cities and {len(stations)} fuel stations.'))
        self.stdout.write(f'Skipped {skipped_non_us} rows outside the USA.')
        if unmatched:
            self.stdout.write(self.style.WARNING(
                f'{len(unmatched)} rows had no coordinates and were skipped: '
                + ', '.join(sorted(f'{c}, {s}' for c, s in unmatched))))

    def read_cities(self, path):
        """Return {(city_key, state): (display_name, lat, lon)}."""
        cities = {}
        try:
            with open(path, encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    key = (city_key(row['CITY']), row['STATE_CODE'].strip())
                    # First occurrence wins (the dataset has rare duplicates).
                    cities.setdefault(key, (row['CITY'].strip(),
                                            float(row['LATITUDE']),
                                            float(row['LONGITUDE'])))
        except FileNotFoundError:
            raise CommandError(f'Cities file not found: {path}')

        for (name, state), (lat, lon) in MANUAL_COORDINATES.items():
            cities.setdefault((name, state), (name.title(), lat, lon))
        return cities

    def read_stations(self, path, cities):
        """Parse the price CSV into FuelStation objects (unsaved).

        The same station ID can appear several times with different prices
        (different supply contracts / rack IDs). We keep one record per
        station, at its lowest listed price.
        """
        best = {}
        skipped_non_us = 0
        unmatched = set()
        try:
            f = open(path, encoding='utf-8-sig')
        except FileNotFoundError:
            raise CommandError(f'Fuel prices file not found: {path}')

        with f:
            for row in csv.DictReader(f):
                state = row['State'].strip()
                if state in NON_US_STATES:
                    skipped_non_us += 1
                    continue

                city = row['City'].strip()
                coords = cities.get((city_key(city), state))
                if coords is None:
                    unmatched.add((city, state))
                    continue

                try:
                    price = Decimal(row['Retail Price'].strip())
                    opis_id = int(row['OPIS Truckstop ID'])
                except (InvalidOperation, ValueError):
                    continue

                current = best.get(opis_id)
                if current is not None and current.price <= price:
                    continue

                rack = row.get('Rack ID', '').strip()
                best[opis_id] = FuelStation(
                    opis_id=opis_id,
                    name=row['Truckstop Name'].strip(),
                    address=row['Address'].strip(),
                    city=city,
                    state=state,
                    rack_id=int(rack) if rack.isdigit() else None,
                    price=price.quantize(Decimal('0.0001')),
                    latitude=coords[1],
                    longitude=coords[2],
                )

        return list(best.values()), skipped_non_us, unmatched
