"""Two one-way syncs on the Google primary calendar, sharing one diff:

- Outlook events                    -> copies in Google primary
- the user's own Google events      -> "BLOCKED" events in Outlook

Each side tags what ogsync writes, and each direction skips the other side's tags,
so nothing loops back.

Google copies keep their state in extendedProperties.private:
  ogsync_source = "outlook"
  ogsync_key    = sha256 of the Outlook ItemId (ids are long and opaque)
  ogsync_hash   = sha256 of the rest of the body, to detect changes

Outlook blocks carry the category OGSYNC_CATEGORY. Their key is derived from their
start/end, so a moved busy period simply becomes delete + insert.
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from ogsync.google_cal import CALENDAR_ID, SOURCE_OUTLOOK
from ogsync.models import Event
from ogsync.owa import DEFAULT_TZ

DETAIL_LEVELS = ("busy", "title", "full")
OGSYNC_CATEGORY = "ogsync"
BLOCK_SUBJECT = "BLOCKED"  # Outlook side, for busy time in Google primary
GOOGLE_BLOCK_SUBJECT = "AD: BLOCKED"  # Google side, for Outlook events at detail "busy"


class UnsafeSync(Exception):
    pass


@dataclass
class Plan[T]:
    inserts: list[T] = field(default_factory=list)
    updates: list[tuple[str, T]] = field(default_factory=list)  # (existing id, new value)
    deletes: list[str] = field(default_factory=list)  # existing ids

    def __bool__(self) -> bool:
        return bool(self.inserts or self.updates or self.deletes)


@dataclass(frozen=True)
class Block:
    start: datetime
    end: datetime


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def diff[T](
    desired: dict[str, T],
    existing: Sequence[tuple[str, str | None, str | None]],
    hash_of: Callable[[T], str | None],
    allow_empty: bool,
    what: str,
) -> Plan[T]:
    """Compare desired {key: value} with existing [(id, key, hash)] items.

    Existing items whose key is no longer desired (or duplicates) are deleted; a
    hash mismatch means update. hash_of returning None disables updates.
    """
    if not desired and existing and not allow_empty:
        raise UnsafeSync(
            f"{what}: 元データが 0 件ですが、同期済みの予定が {len(existing)} 件あります。"
            "取得失敗の可能性があるため中止しました（本当に空なら --allow-empty）。"
        )
    plan: Plan[T] = Plan()
    seen: set[str] = set()
    for item_id, key, h in existing:
        if key not in desired or key in seen:
            plan.deletes.append(item_id)
            continue
        seen.add(key)
        new_hash = hash_of(desired[key])
        if new_hash is not None and new_hash != h:
            plan.updates.append((item_id, desired[key]))
    plan.inserts = [v for k, v in desired.items() if k not in seen]
    return plan


# ---------------------------------------------------------------- Outlook -> Google


def outlook_key(e: Event) -> str:
    return _sha(e.source_id)


def to_google_body(e: Event, detail: str = "busy", tz: tzinfo = DEFAULT_TZ) -> dict:
    if e.is_all_day:
        start = {"date": e.start.astimezone(tz).date().isoformat()}
        end = {"date": e.end.astimezone(tz).date().isoformat()}  # exclusive, like Outlook
    else:
        start = {"dateTime": e.start.isoformat(), "timeZone": str(tz)}
        end = {"dateTime": e.end.isoformat(), "timeZone": str(tz)}
    body = {
        "summary": GOOGLE_BLOCK_SUBJECT if detail == "busy" else (e.subject or "(件名なし)"),
        "start": start,
        "end": end,
        "transparency": "transparent" if e.show_as.lower() == "free" else "opaque",
        # The work calendar already notifies; avoid duplicate reminders on the phone.
        "reminders": {"useDefault": False, "overrides": []},
    }
    if detail == "full" and e.location:
        body["location"] = e.location
    body["extendedProperties"] = {
        "private": {
            "ogsync_source": SOURCE_OUTLOOK,
            "ogsync_key": outlook_key(e),
            "ogsync_hash": _sha(json.dumps(body, sort_keys=True, ensure_ascii=False)),
        }
    }
    return body


def _private(item: dict) -> dict:
    return item.get("extendedProperties", {}).get("private", {})


def plan_to_google(
    outlook_events: list[Event],
    google_items: list[dict],
    detail: str = "busy",
    allow_empty: bool = False,
) -> Plan[dict]:
    desired = {
        outlook_key(e): to_google_body(e, detail)
        for e in outlook_events
        # Skip our own BLOCKED events, or primary's busy time would loop back into Google.
        if not e.is_cancelled and OGSYNC_CATEGORY not in e.categories
    }
    existing = [
        (item["id"], _private(item).get("ogsync_key"), _private(item).get("ogsync_hash"))
        for item in google_items
    ]
    return diff(
        desired,
        existing,
        lambda body: body["extendedProperties"]["private"]["ogsync_hash"],
        allow_empty,
        "Outlook → Google",
    )


def apply_to_google(svc, p: Plan[dict]) -> None:
    # Ids in p come only from list_synced, i.e. events tagged as our Outlook copies.
    events = svc.events()
    for body in p.inserts:
        events.insert(calendarId=CALENDAR_ID, body=body).execute()
    for google_id, body in p.updates:
        events.update(calendarId=CALENDAR_ID, eventId=google_id, body=body).execute()
    for google_id in p.deletes:
        events.delete(calendarId=CALENDAR_ID, eventId=google_id).execute()


# ---------------------------------------------------------------- Google -> Outlook


def block_key(start: datetime, end: datetime) -> str:
    return _sha(f"{start.timestamp():.0f}-{end.timestamp():.0f}")


def _declined(item: dict) -> bool:
    return any(
        a.get("self") and a.get("responseStatus") == "declined" for a in item.get("attendees", [])
    )


def busy_from_google(items: list[dict], lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
    """Merged busy periods of the user's own timed Google events overlapping [lo, hi)."""
    periods = sorted(
        (datetime.fromisoformat(i["start"]["dateTime"]), datetime.fromisoformat(i["end"]["dateTime"]))
        for i in items
        if _private(i).get("ogsync_source") != SOURCE_OUTLOOK  # our copies of Outlook events
        and i.get("status") != "cancelled"
        and i.get("transparency") != "transparent"  # marked "free"
        and not _declined(i)
        and "dateTime" in i.get("start", {})  # all-day events have "date" instead
    )
    merged: list[tuple[datetime, datetime]] = []
    for s, e in periods:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return [(s, e) for s, e in merged if e > lo and s < hi]


def plan_to_outlook(
    busy: list[tuple[datetime, datetime]],
    outlook_events: list[Event],
    allow_empty: bool = False,
) -> Plan[Block]:
    desired = {block_key(s, e): Block(s, e) for s, e in busy}
    existing = [
        (e.source_id, block_key(e.start, e.end), None)
        for e in outlook_events
        if OGSYNC_CATEGORY in e.categories
    ]
    # A block's content is fully determined by its key, so it never needs updating.
    return diff(desired, existing, lambda _: None, allow_empty, "Google → Outlook")


def apply_to_outlook(client, p: Plan[Block], tz: tzinfo = DEFAULT_TZ) -> None:
    # Ids in p.deletes come only from Outlook events carrying OGSYNC_CATEGORY.
    for block in p.inserts:
        client.create_event(BLOCK_SUBJECT, block.start, block.end, OGSYNC_CATEGORY, tz)
    for item_id in p.deletes:
        client.delete_event(item_id)
