from datetime import datetime, timezone

import pytest

from ogsync import sync
from ogsync.models import Event


def ev(source_id: str, subject: str = "会議", hour: int = 1, **kw) -> Event:
    return Event(
        source_id=source_id,
        subject=subject,
        start=datetime(2026, 10, 23, hour, tzinfo=timezone.utc),
        end=datetime(2026, 10, 23, hour + 1, tzinfo=timezone.utc),
        **kw,
    )


def as_google(google_id: str, e: Event, detail: str = "busy") -> dict:
    """What Google returns for an event we inserted earlier."""
    return {"id": google_id, **sync.to_google_body(e, detail)}


def test_insert_new_events():
    p = sync.plan_to_google([ev("a"), ev("b")], [])
    assert len(p.inserts) == 2 and not p.updates and not p.deletes


def test_unchanged_event_is_left_alone():
    p = sync.plan_to_google([ev("a")], [as_google("g1", ev("a"))])
    assert not p


def test_changed_event_is_updated():
    p = sync.plan_to_google([ev("a", hour=3)], [as_google("g1", ev("a"))])
    assert [gid for gid, _ in p.updates] == ["g1"] and not p.inserts and not p.deletes


def test_default_summary_is_fixed():
    assert sync.to_google_body(ev("a", subject="社外秘の会議"))["summary"] == "AD: BLOCKED"


def test_detail_change_triggers_update():
    p = sync.plan_to_google([ev("a")], [as_google("g1", ev("a"))], detail="title")
    assert p.updates[0][1]["summary"] == "会議"


def test_removed_and_cancelled_events_are_deleted():
    google = [as_google("g1", ev("a")), as_google("g2", ev("b"))]
    p = sync.plan_to_google([ev("a"), ev("b", is_cancelled=True)], google)
    assert p.deletes == ["g2"]


def test_duplicates_are_deleted():
    google = [as_google("g1", ev("a")), as_google("g2", ev("a"))]
    p = sync.plan_to_google([ev("a")], google)
    assert p.deletes == ["g2"] and not p.inserts


def test_empty_outlook_is_refused():
    with pytest.raises(sync.UnsafeSync):
        sync.plan_to_google([], [as_google("g1", ev("a"))])
    assert sync.plan_to_google([], [as_google("g1", ev("a"))], allow_empty=True).deletes == ["g1"]


def test_all_day_uses_local_dates():
    e = Event(
        source_id="d",
        subject="休暇",
        start=datetime(2026, 10, 23, 15, tzinfo=timezone.utc),  # 10/24 00:00 JST
        end=datetime(2026, 10, 24, 15, tzinfo=timezone.utc),
        is_all_day=True,
    )
    body = sync.to_google_body(e)
    assert body["start"] == {"date": "2026-10-24"} and body["end"] == {"date": "2026-10-25"}


def test_free_is_transparent_and_location_only_in_full():
    e = ev("a", show_as="Free", location="会議室A")
    assert sync.to_google_body(e)["transparency"] == "transparent"
    assert "location" not in sync.to_google_body(e, "title")
    assert sync.to_google_body(e, "full")["location"] == "会議室A"


def t(hour: int, day: int = 23) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def test_own_blocks_are_not_copied_to_google():
    blocked = ev("x", subject="BLOCKED", categories=(sync.OGSYNC_CATEGORY,))
    p = sync.plan_to_google([ev("a"), blocked], [], detail="title")
    assert [b["summary"] for b in p.inserts] == ["会議"]


def outlook_block(source_id: str, start: datetime, end: datetime) -> Event:
    return Event(source_id, "BLOCKED", start, end, categories=(sync.OGSYNC_CATEGORY,))


def test_busy_blocks_are_inserted():
    p = sync.plan_to_outlook([(t(1), t(2)), (t(5), t(6))], [])
    assert p.inserts == [sync.Block(t(1), t(2)), sync.Block(t(5), t(6))]


def test_existing_block_kept_and_moved_block_replaced():
    existing = [outlook_block("o1", t(1), t(2)), outlook_block("o2", t(5), t(6))]
    p = sync.plan_to_outlook([(t(1), t(2)), (t(7), t(8))], existing)
    assert p.inserts == [sync.Block(t(7), t(8))]
    assert p.deletes == ["o2"] and not p.updates


def test_regular_outlook_events_are_never_deleted():
    p = sync.plan_to_outlook([(t(1), t(2))], [ev("work", hour=1)])
    assert p.inserts == [sync.Block(t(1), t(2))] and not p.deletes


def g(start: datetime, end: datetime, **kw) -> dict:
    return {"id": "x", "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}, **kw}


LO, HI = t(0), t(0, 24)


def test_busy_skips_copies_free_declined_cancelled_and_all_day():
    items = [
        g(t(1), t(2)),
        {"id": "copy", **sync.to_google_body(ev("a", hour=3))},
        g(t(5), t(6), transparency="transparent"),
        g(t(7), t(8), attendees=[{"self": True, "responseStatus": "declined"}]),
        g(t(9), t(10), status="cancelled"),
        {"id": "allday", "start": {"date": "2026-10-23"}, "end": {"date": "2026-10-24"}},
    ]
    assert sync.busy_from_google(items, LO, HI) == [(t(1), t(2))]


def test_busy_merges_overlapping_and_adjacent_events():
    items = [g(t(1), t(3)), g(t(2), t(4)), g(t(4), t(5)), g(t(7), t(8))]
    assert sync.busy_from_google(items, LO, HI) == [(t(1), t(5)), (t(7), t(8))]


def test_busy_keeps_blocks_crossing_window_edge_unclipped():
    items = [g(t(22, 22), t(2, 23))]
    assert sync.busy_from_google(items, LO, HI) == [(t(22, 22), t(2, 23))]


def test_empty_primary_with_blocks_is_refused():
    with pytest.raises(sync.UnsafeSync):
        sync.plan_to_outlook([], [outlook_block("o1", t(1), t(2))])
