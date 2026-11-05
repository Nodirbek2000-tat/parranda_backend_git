"""Bird counts reconstructed from movements and per-household aggregation (API_CONTRACT_V2 §1.1, §7.2).

Everything here is pure Python over rows already fetched by :mod:`stats.queries` (no database access),
so it is unit tested with in-memory rows (including the worked example of §1.3).

Day indices: index ``0`` is the first day of the loaded window (``start``); a movement dated ``d``
changes the end-of-day count from day ``d`` on (``B_h(d)`` counts movements with ``date ≤ d``).
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from farm.models import PoultryType

from .metrics import Totals

POULTRY_TYPES: tuple[str, ...] = (PoultryType.BROILER, PoultryType.BREEDER)


class Step:
    """Piecewise constant integer function of the day index (an end-of-day bird count).

    Built from an opening value (everything before index 0) and net changes per day index; evaluating
    or summing it is ``O(log changes)`` / ``O(changes)``, never ``O(days)``.
    """

    __slots__ = ("starts", "values")

    def __init__(self, opening: int = 0, changes: Mapping[int, int] | None = None) -> None:
        starts = [0]
        values = [opening]
        for index in sorted(changes or ()):
            delta = changes[index]
            if not delta:
                continue
            if index <= starts[-1]:
                values[-1] += delta
            else:
                starts.append(index)
                values.append(values[-1] + delta)
        self.starts = starts
        self.values = values

    def at(self, index: int) -> int:
        if len(self.starts) == 1:
            return self.values[0]
        return self.values[bisect_right(self.starts, index) - 1]

    def _segments(self, first: int, last: int) -> Iterable[tuple[int, int, int]]:
        """``(lo, hi, value)`` pieces covering ``first … last`` (inclusive)."""
        if first > last:
            return
        starts, values = self.starts, self.values
        k = bisect_right(starts, first) - 1
        count = len(starts)
        while k < count and starts[k] <= last:
            lo = max(starts[k], first)
            hi = min(starts[k + 1] - 1, last) if k + 1 < count else last
            yield lo, hi, values[k]
            k += 1

    def total(self, first: int, last: int) -> int:
        """``Σ_{i=first..last} value(i)`` (bird-days)."""
        return sum((hi - lo + 1) * value for lo, hi, value in self._segments(first, last))

    def positive_ranges(self, first: int, last: int) -> list[tuple[int, int]]:
        return [(lo, hi) for lo, hi, value in self._segments(first, last) if value > 0]

    def positive_days(self, first: int, last: int) -> int:
        return sum(hi - lo + 1 for lo, hi in self.positive_ranges(first, last))


@dataclass(frozen=True)
class HouseholdMeta:
    id: int
    name: str
    region: str
    district: str
    is_active: bool
    created_day: date

    @property
    def code(self) -> str:
        return f"XN-{self.id:04d}"

    def short(self) -> dict:
        return {"id": self.id, "code": self.code, "name": self.name}


@dataclass(frozen=True, slots=True)
class Flow:
    """Household movement sums of one ``(day, poultry type)``."""

    index: int
    poultry_type: str
    deaths: int
    distributed: int
    returned: int
    out: int


class FlockWindow:
    """Bird counts of households for the window ``start … end`` built from two aggregate queries.

    ``opening_rows``: ``(household_id | None, poultry_type, household_net, center_net)`` for ``date < start``.
    ``daily_rows``: ``(household_id | None, poultry_type, date, household_net, center_net, deaths,
    distributed, returned, out)`` for ``start ≤ date ≤ end``. Rows with ``household_id = None`` are
    center movements.
    """

    def __init__(self, start: date, end: date, opening_rows: Iterable[tuple], daily_rows: Iterable[tuple]) -> None:
        self.start = start
        self.end = end
        self.length = (end - start).days + 1
        self.opening: dict[tuple[int, str], int] = defaultdict(int)
        self.changes: dict[tuple[int, str], dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self.flows: dict[int, list[Flow]] = defaultdict(list)
        self.center_flows: list[tuple[int, int, int]] = []  # (index, deaths, out)
        self.center_net = 0  # Σ center deltas of all movements dated ≤ end
        for household_id, poultry_type, household_net, center_net in opening_rows:
            self.center_net += center_net or 0
            if household_id is not None:
                self.opening[(household_id, poultry_type)] += household_net or 0
        for row in daily_rows:
            household_id, poultry_type, day, household_net, center_net, deaths, distributed, returned, out = row
            index = (day - start).days
            self.center_net += center_net or 0
            if household_id is None:
                self.center_flows.append((index, deaths or 0, out or 0))
                continue
            self.changes[(household_id, poultry_type)][index] += household_net or 0
            self.flows[household_id].append(
                Flow(index, poultry_type, deaths or 0, distributed or 0, returned or 0, out or 0)
            )
        self._types: dict[int, set[str]] = defaultdict(set)
        for household_id, poultry_type in (*self.opening, *self.changes):
            self._types[household_id].add(poultry_type)

    def index(self, day: date) -> int:
        return (day - self.start).days

    def day(self, index: int) -> date:
        return self.start + timedelta(days=index)

    def types_of(self, household_id: int) -> set[str]:
        return self._types.get(household_id, set())

    def step(self, household_id: int, poultry_type: str) -> Step:
        key = (household_id, poultry_type)
        return Step(self.opening.get(key, 0), self.changes.get(key))

    def step_all(self, household_id: int) -> Step:
        opening = 0
        merged: dict[int, int] = defaultdict(int)
        for poultry_type in self.types_of(household_id):
            key = (household_id, poultry_type)
            opening += self.opening.get(key, 0)
            for index, delta in self.changes.get(key, {}).items():
                merged[index] += delta
        return Step(opening, merged)

    def center_between(self, first: int, last: int) -> tuple[int, int]:
        deaths = out = 0
        for index, row_deaths, row_out in self.center_flows:
            if first <= index <= last:
                deaths += row_deaths
                out += row_out
        return deaths, out


@dataclass
class HouseholdState:
    meta: HouseholdMeta
    all: Step
    by_type: dict[str, Step]
    created_index: int
    current: Totals = field(default_factory=Totals)
    previous: Totals = field(default_factory=Totals)
    type_totals: dict[str, Totals] = field(default_factory=dict)


@dataclass
class DailySeries:
    """Per-day sums of the current period (index 0 = first day of the period)."""

    birds: list[int]
    meat_cents: list[int]
    feed_cents: list[int]
    deaths: list[int]
    distributed: list[int]
    returned: list[int]
    reports: list[int]
    expected: list[int]
    reported_expected: list[int]
    weight_bird_cents: list[int]
    weighed_bird_days: list[int]

    @classmethod
    def empty(cls, days: int) -> DailySeries:
        return cls(*([0] * days for _ in range(11)))


class ScopeStats:
    """Aggregates of a set of households for the current period (and optionally the previous one).

    ``current = (first, last)`` and ``previous`` are inclusive day-index ranges inside the window.
    Report rows: ``(household_id, day index from window.start, meat_cents, feed_cents, weight_cents)``
    (see ``queries.report_rows``; ``weight_cents < 0`` = no weight stated).

    ``today`` (V2 §1.1 “today is not over”): when the current period contains today, today is an expected
    day of the PERIOD aggregates (``Totals.expected`` / ``reported_expected`` → discipline, missing days)
    only once its report exists. The per-day series keep ``expected`` = households due today, so the
    “so far” state of today stays visible (``reported_expected[today]`` / ``expected[today]``).
    """

    def __init__(
        self,
        window: FlockWindow,
        households: Iterable[HouseholdMeta],
        reports: Iterable[tuple[int, int, int, int, int]],
        *,
        current: tuple[int, int],
        previous: tuple[int, int] | None = None,
        today: date | None = None,
    ) -> None:
        self.window = window
        self.current = current
        self.previous = previous
        self.days = current[1] - current[0] + 1
        today_index = window.index(today) if today is not None else None
        #: Window index of today when it lies in the current period (else ``None``).
        self.today_index = today_index if today_index is not None and current[0] <= today_index <= current[1] else None
        self.states: dict[int, HouseholdState] = {}
        for meta in households:
            by_type = {poultry_type: window.step(meta.id, poultry_type) for poultry_type in window.types_of(meta.id)}
            self.states[meta.id] = HouseholdState(
                meta=meta,
                all=window.step_all(meta.id),
                by_type=by_type,
                created_index=window.index(meta.created_day),
            )
        self.series = DailySeries.empty(self.days)
        self._movement_totals()
        self._report_totals(reports)

    # -- movements ---------------------------------------------------------------------------

    def _fill_movements(self, state: HouseholdState, totals: Totals, first: int, last: int) -> None:
        totals.bird_days = state.all.total(first, last)
        totals.birds_end = state.all.at(last)
        for flow in self.window.flows.get(state.meta.id, ()):
            if first <= flow.index <= last:
                totals.deaths += flow.deaths
                totals.distributed += flow.distributed
                totals.returned += flow.returned
        if state.meta.is_active:
            # Today joins the period's expected days only when reported (added in _report_totals).
            last_due = last - 1 if self.today_index == last else last
            totals.expected = state.all.positive_days(max(first, state.created_index), last_due)

    def _movement_totals(self) -> None:
        first, last = self.current
        series = self.series
        birds_delta = [0] * (self.days + 1)
        expected_delta = [0] * (self.days + 1)
        for state in self.states.values():
            self._fill_movements(state, state.current, first, last)
            if self.previous is not None:
                self._fill_movements(state, state.previous, *self.previous)
            for poultry_type, step in state.by_type.items():
                type_totals = Totals(bird_days=step.total(first, last), birds_end=step.at(last))
                for flow in self.window.flows.get(state.meta.id, ()):
                    if flow.poultry_type == poultry_type and first <= flow.index <= last:
                        type_totals.deaths += flow.deaths
                        type_totals.distributed += flow.distributed
                        type_totals.returned += flow.returned
                state.type_totals[poultry_type] = type_totals
            for lo, hi, value in state.all._segments(first, last):
                birds_delta[lo - first] += value
                birds_delta[hi - first + 1] -= value
            if state.meta.is_active:
                for lo, hi in state.all.positive_ranges(max(first, state.created_index), last):
                    expected_delta[lo - first] += 1
                    expected_delta[hi - first + 1] -= 1
            for flow in self.window.flows.get(state.meta.id, ()):
                if first <= flow.index <= last:
                    j = flow.index - first
                    series.deaths[j] += flow.deaths
                    series.distributed[j] += flow.distributed
                    series.returned[j] += flow.returned
        running_birds = running_expected = 0
        for j in range(self.days):
            running_birds += birds_delta[j]
            running_expected += expected_delta[j]
            series.birds[j] = running_birds
            series.expected[j] = running_expected

    # -- reports -----------------------------------------------------------------------------

    def _report_totals(self, reports: Iterable[tuple[int, int, int, int, int]]) -> None:
        """One pass over ``(household_id, day index, meat_cents, feed_cents, weight_cents)`` rows (the hot loop:
        ~70k rows/year)."""
        current_first, current_last = self.current
        previous_first, previous_last = self.previous if self.previous is not None else (1, 0)
        series = self.series
        never = self.window.length + 1
        lookup = {}
        for household_id, state in self.states.items():
            lookup[household_id] = (
                state,
                state.all.starts,
                state.all.values,
                state.created_index if state.meta.is_active else never,
            )
        self.reported_days: set[int] = set()  # current-period indices with a report (single household use)
        today_index = self.today_index if self.today_index is not None else -1
        for household_id, index, meat_cents, feed_cents, weight_cents in reports:
            entry = lookup.get(household_id)
            if entry is None:
                continue
            state, starts, values, expected_from = entry
            if current_first <= index <= current_last:
                totals = state.current
                j = index - current_first
            elif previous_first <= index <= previous_last:
                totals = state.previous
                j = -1
            else:
                continue
            birds = values[0] if len(starts) == 1 else values[bisect_right(starts, index) - 1]
            totals.meat_cents += meat_cents
            totals.feed_cents += feed_cents
            totals.reports += 1
            weighed = birds > 0 and weight_cents >= 0
            if weighed:
                totals.weight_bird_cents += weight_cents * birds
                totals.weighed_bird_days += birds
            due = False
            if birds > 0:
                totals.rp_feed_cents += feed_cents
                totals.rp_bird_days += birds
                if index >= expected_from:
                    due = True
                    totals.reported_expected += 1
                    if index == today_index:
                        totals.expected += 1  # today counts once its report exists
            if j >= 0:
                series.meat_cents[j] += meat_cents
                series.feed_cents[j] += feed_cents
                series.reports[j] += 1
                self.reported_days.add(j)
                if due:
                    series.reported_expected[j] += 1
                if weighed:
                    series.weight_bird_cents[j] += weight_cents * birds
                    series.weighed_bird_days[j] += birds

    # -- helpers -----------------------------------------------------------------------------

    def total_current(self, household_ids: Iterable[int] | None = None) -> Totals:
        ids = self.states.keys() if household_ids is None else household_ids
        return Totals.merged(self.states[i].current for i in ids)

    def total_previous(self) -> Totals:
        return Totals.merged(state.previous for state in self.states.values())

    def type_totals(self, poultry_type: str) -> tuple[int, Totals]:
        """``(households with birds of the type at the end, merged totals of the type)``."""
        merged = Totals()
        households = 0
        for state in self.states.values():
            totals = state.type_totals.get(poultry_type)
            if totals is None:
                continue
            merged.add(totals)
            if totals.birds_end > 0:
                households += 1
        return households, merged
