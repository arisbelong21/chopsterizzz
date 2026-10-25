"""Publish-time planning: staggered batches and Auto-Pilot slot assignment.

This module is pure (no DB, no threads, no Qt) so the exact same arithmetic is
used by the UI preview, the batch engine and the Auto-Pilot rule engine. That is
deliberate: spec 5.6 forbids faking a schedule, so an impossible request raises
instead of silently collapsing into something that merely looks valid.

Weekday numbering follows Python's ``datetime.weekday()`` (Monday == 0).
"""
from __future__ import annotations

from datetime import datetime, timedelta

WEEKDAY_LABELS = ("Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min")
ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)


class PlanError(ValueError):
    """Raised when a scheduling request cannot produce valid, non-colliding slots."""

    def __init__(self, message: str, user_message: str = "") -> None:
        super().__init__(message)
        self.user_message = user_message or message


def _normalize_days(active_days) -> set[int]:
    if active_days is None:
        return set(ALL_DAYS)
    days = {int(d) for d in active_days}
    invalid = {d for d in days if d < 0 or d > 6}
    if invalid:
        raise PlanError(f"invalid weekday numbers: {sorted(invalid)}",
                        "Hari aktif tidak valid.")
    return days or set(ALL_DAYS)


def _normalize_hours(active_hours) -> tuple[int, int] | None:
    if not active_hours:
        return None
    try:
        lo, hi = int(active_hours[0]), int(active_hours[1])
    except Exception as exc:  # noqa: BLE001 - reported as user-facing validation
        raise PlanError(f"bad active_hours: {active_hours!r}",
                        "Jam operasional tidak valid.") from exc
    if not (0 <= lo < hi <= 24):
        raise PlanError(f"active_hours out of range: {(lo, hi)}",
                        "Jam operasional harus 0-24 dan jam mulai lebih awal dari jam selesai.")
    return (lo, hi)


def _day_start(dt: datetime, hour: int, minute: int = 0) -> datetime:
    return (dt + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)


def _next_day(dt: datetime, hour: int, minute: int) -> datetime:
    return _day_start(dt, hour, minute)


def _clamp_to_window(dt: datetime, win: tuple[int, int] | None, days: set[int],
                     max_hops: int, anchor_hour: int = 0, anchor_minute: int = 0) -> datetime:
    """Roll ``dt`` forward until it sits inside an active day and active hour window.

    Without an operating window, a new day starts at the caller's anchor
    time-of-day (the requested start) rather than midnight, so a user asking for
    08:00 publishing never receives 00:00 slots on later days.
    """
    cur = dt
    hops = 0
    while True:
        hops += 1
        if hops > max_hops:
            raise PlanError("could not fit inside the operating window",
                            "Tidak ada slot valid di dalam jam/hari operasional yang dipilih.")
        if cur.weekday() not in days:
            if win is None:
                cur = _next_day(cur, anchor_hour, anchor_minute)
            else:
                cur = _next_day(cur, win[0], 0)
            continue
        if win is None:
            return cur
        if cur.hour < win[0]:
            cur = cur.replace(hour=win[0], minute=0, second=0, microsecond=0)
            continue
        if cur.hour >= win[1]:
            cur = _next_day(cur, win[0], 0)
            continue
        return cur


def compute_staggered_slots(*, start: datetime, count: int, interval_minutes: int = 120,
                            active_hours: tuple[int, int] | None = None,
                            active_days=None, max_lookahead_days: int = 180) -> list[datetime]:
    """Return ``count`` strictly increasing, non-colliding publish times.

    ``start`` is the first candidate; when it falls outside the active window the
    first slot rolls forward to the next valid moment (next window opening, or the
    next active day at the same time-of-day). Raising :class:`PlanError` is the
    honest outcome when the request is unsatisfiable.
    """
    if int(count) < 1:
        raise PlanError("count must be >= 1", "Jumlah video harus minimal 1.")
    if int(interval_minutes) < 1:
        raise PlanError("interval must be >= 1 minute", "Interval minimal 1 menit.")
    if int(count) * int(interval_minutes) > int(max_lookahead_days) * 24 * 60:
        raise PlanError("schedule exceeds lookahead",
                        "Jadwal terlalu panjang untuk jumlah video ini. Perkecil interval.")

    days = _normalize_days(active_days)
    win = _normalize_hours(active_hours)
    anchor = start.replace(second=0, microsecond=0)
    limit = anchor + timedelta(days=int(max_lookahead_days))
    step = timedelta(minutes=int(interval_minutes))

    cur = _clamp_to_window(anchor, win, days, 400,
                           anchor_hour=anchor.hour, anchor_minute=anchor.minute)
    slots: list[datetime] = []
    while len(slots) < int(count):
        if cur > limit:
            raise PlanError("no slot found within lookahead",
                            "Tidak bisa menemukan slot jadwal. Longgarkan jam/hari operasional.")
        slots.append(cur)
        cur = _clamp_to_window(cur + step, win, days, 400,
                               anchor_hour=anchor.hour, anchor_minute=anchor.minute)

    # Invariant: ordered and collision-free, or we refuse to hand out a schedule.
    if len(set(slots)) != len(slots):
        raise PlanError("slot collision detected",
                        "Jadwal menghasilkan waktu bentrok. Perbesar interval.")
    for earlier, later in zip(slots, slots[1:]):
        if later <= earlier:
            raise PlanError("slots are not strictly increasing",
                            "Jadwal tidak berurutan. Periksa interval dan jam operasional.")
    return slots


def plan_rule_slot(*, now: datetime, start_time: str = "10:00", interval_minutes: int = 120,
                   used_today: int = 0, per_day: int = 1,
                   active_hours: tuple[int, int] | None = None, active_days=None,
                   missed_policy: str = "next_slot", max_lookahead_days: int = 60) -> datetime:
    """Pick the execution time for one Auto-Pilot job.

    ``used_today`` is how many jobs this rule already created *today*, so slot
    ``used_today`` of the daily pattern is used; when the day is full the pattern
    restarts on the next active day. ``missed_policy``:

      * ``next_slot`` - a slot already in the past moves to the next one,
      * ``run_now``   - publish as soon as possible instead of dropping it.
    """
    if int(per_day) < 1:
        raise PlanError("per_day must be >= 1", "Posting per hari minimal 1.")
    if int(used_today) < 0:
        raise PlanError("used_today must be >= 0", "Jumlah job hari ini tidak valid.")
    policy = str(missed_policy or "next_slot").lower()
    if policy not in ("next_slot", "run_now"):
        raise PlanError(f"unknown missed_policy: {policy}",
                        "Kebijakan jadwal terlewat tidak dikenal.")

    days = _normalize_days(active_days)
    win = _normalize_hours(active_hours)
    try:
        hh, mm = str(start_time or "10:00").split(":")
        base_hour, base_min = int(hh), int(mm)
        if not (0 <= base_hour <= 23 and 0 <= base_min <= 59):
            raise ValueError(start_time)
    except Exception as exc:  # noqa: BLE001
        raise PlanError(f"bad start_time: {start_time!r}",
                        "Jam mulai tidak valid (format HH:MM).") from exc

    interval = int(interval_minutes)
    if interval < 1:
        raise PlanError("interval must be >= 1 minute", "Interval minimal 1 menit.")

    day = now.replace(hour=base_hour, minute=base_min, second=0, microsecond=0)
    position = int(used_today)
    hops = 0
    while True:
        hops += 1
        if hops > int(max_lookahead_days) * 8:
            raise PlanError("no rule slot found",
                            "Rule tidak menemukan slot jadwal. Periksa jam/hari operasional.")
        if day.weekday() not in days:
            day, position = _next_day(day, base_hour, base_min), 0
            continue
        if position >= int(per_day):
            day, position = _next_day(day, base_hour, base_min), 0
            continue
        candidate = day + timedelta(minutes=position * interval)
        if win is not None and not (win[0] <= candidate.hour < win[1]):
            day, position = _next_day(day, base_hour, base_min), 0
            continue
        if candidate >= now:
            return candidate
        # The slot already passed while the app was closed or the rule disabled.
        if policy == "run_now":
            return _clamp_to_window(now.replace(second=0, microsecond=0), win, days, 400,
                                    anchor_hour=base_hour, anchor_minute=base_min)
        position += 1


def describe_slots(slots: list[datetime], limit: int = 4) -> str:
    """Compact human preview used by the Studio UI."""
    if not slots:
        return "-"
    shown = [dt.strftime("%a %d %b %H:%M") for dt in slots[:limit]]
    text = " | ".join(shown)
    if len(slots) > limit:
        text += f" ... (+{len(slots) - limit} lagi, sampai {slots[-1].strftime('%d %b %H:%M')})"
    return text


def describe_days(active_days) -> str:
    days = _normalize_days(active_days)
    if days == set(ALL_DAYS):
        return "Setiap hari"
    return ", ".join(WEEKDAY_LABELS[d] for d in sorted(days))