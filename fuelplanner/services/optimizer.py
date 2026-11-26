"""Pick fuel stops minimising: fuel cost + stop_penalty per stop.

Dynamic programming over (station, reach), where reach = mile marker + fuel
in miles. The penalty only guides the choice; it is not in the reported cost.
"""

import math
from dataclasses import dataclass, field

import numpy as np

EPSILON = 1e-9


class NoFeasiblePlanError(Exception):
    """The vehicle cannot reach the destination with the available stations."""


@dataclass(frozen=True)
class FuelStop:
    station: object          # anything with .mile_marker and .price
    gallons: float
    cost: float

    @property
    def mile_marker(self):
        return self.station.mile_marker


@dataclass
class FuelPlan:
    stops: list = field(default_factory=list)
    total_gallons: float = 0.0
    total_cost: float = 0.0


def plan_fuel_stops(stations, total_miles, tank_range_miles=500.0, mpg=10.0,
                    start_fuel_miles=None, stop_penalty=0.0):
    """Return the cheapest FuelPlan. Starts with a full tank unless start_fuel_miles is set."""
    if tank_range_miles < 1 or mpg <= 0 or stop_penalty < 0:
        raise ValueError('tank_range_miles must be >= 1, mpg > 0, stop_penalty >= 0.')
    tank = int(math.floor(tank_range_miles + EPSILON))
    start_fuel = tank_range_miles if start_fuel_miles is None else start_fuel_miles
    if not 0 <= start_fuel <= tank_range_miles:
        raise ValueError('start_fuel_miles must be between 0 and tank_range_miles.')

    stations = sorted((s for s in stations if 0 <= s.mile_marker <= total_miles),
                      key=lambda s: s.mile_marker)
    finish = int(math.ceil(total_miles - EPSILON))

    # cost[r] = cheapest cost so far with reach r.
    size = finish + tank + 2
    cost = np.full(size, np.inf)
    cost[int(math.floor(start_fuel + EPSILON))] = 0.0
    decisions = []  # per station: (lo, reach before buying, or -1)

    for station in stations:
        lo = int(math.ceil(station.mile_marker - EPSILON))
        cost[:lo] = np.inf  # can't reach this station
        if not np.isfinite(cost[lo:]).any():
            raise NoFeasiblePlanError(_explain_gap(stations, total_miles, tank_range_miles,
                                                   start_fuel))
        hi = min(int(math.floor(station.mile_marker + tank + EPSILON)), size - 1)

        window = cost[lo:hi + 1]
        reach = np.arange(lo, hi + 1)
        per_mile = float(station.price) / mpg
        values = window - reach * per_mile
        running = np.minimum.accumulate(values)
        positions = np.arange(values.size)
        best_from = np.maximum.accumulate(np.where(values <= running, positions, 0))
        stop_cost = running + reach * per_mile + stop_penalty

        better = stop_cost < window - EPSILON
        cost[lo:hi + 1] = np.where(better, stop_cost, window)
        decisions.append((lo, np.where(better, lo + best_from, -1)))

    arrived = cost[finish:]
    if not np.isfinite(arrived).any():
        raise NoFeasiblePlanError(_explain_gap(stations, total_miles, tank_range_miles,
                                               start_fuel))

    # Walk back through decisions to find the purchases.
    reach = finish + int(np.argmin(arrived))
    purchases = []
    for station, (lo, source) in zip(reversed(stations), reversed(decisions)):
        k = reach - lo
        if 0 <= k < source.size and source[k] >= 0:
            purchases.append((station, reach - int(source[k])))
            reach = int(source[k])

    plan = FuelPlan()
    for station, miles in reversed(purchases):
        gallons = miles / mpg
        cost_here = gallons * float(station.price)
        plan.stops.append(FuelStop(station=station, gallons=gallons, cost=cost_here))
        plan.total_gallons += gallons
        plan.total_cost += cost_here
    return plan


def _explain_gap(stations, total_miles, tank_range_miles, start_fuel):
    """Human-readable reason why no plan exists."""
    miles = [s.mile_marker for s in stations]
    if not miles or miles[0] > start_fuel + EPSILON:
        first = f'mile {miles[0]:.0f}' if miles else 'none on route'
        return (f'Cannot reach the first fuel station ({first}) '
                f'with {start_fuel:.0f} miles of fuel.')
    points = miles + [total_miles]
    for a, b in zip(points, points[1:]):
        if b - a > tank_range_miles + EPSILON:
            target = 'the destination' if b == total_miles else f'mile {b:.0f}'
            return (f'No fuel station within {tank_range_miles:.0f} miles of mile {a:.0f}; '
                    f'the next option is {target}.')
    return 'No feasible fuel plan for this route.'
