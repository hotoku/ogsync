"""Read calendar events from Outlook on the web (OWA) by browser automation.

Rather than scraping the rendered DOM, we record the JSON that OWA itself fetches
while showing the calendar, and pick event-shaped objects out of it. Two shapes are
recognized:

- EWS-style JSON from /owa/service.svc (GetCalendarView etc.):
  {"ItemId": {"Id": ...}, "Subject": ..., "Start": "2026-10-23T10:00:00+09:00", ...}
- REST/Graph-style JSON:
  {"Id": ..., "Subject": ..., "Start": {"DateTime": ..., "TimeZone": "UTC"}, ...}
"""

import json
import re
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from playwright.sync_api import BrowserContext, Page, Request, Response

from ogsync.models import Event
from ogsync.owa_api import OwaApiError, OwaClient, default_calendar_id, view_calendar_id

DEFAULT_BASE_URL = "https://outlook.office.com"
OWA_HOSTS = ("outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft")
LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com")
DEFAULT_TZ = ZoneInfo("Asia/Tokyo")

# Windows time zone names that OWA may put in REST-style payloads.
_WINDOWS_TZ = {
    "Tokyo Standard Time": "Asia/Tokyo",
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "Pacific Standard Time": "America/Los_Angeles",
    "Eastern Standard Time": "America/New_York",
}


class SessionExpired(Exception):
    pass


class FetchIncomplete(Exception):
    pass


# ---------------------------------------------------------------- parsing


def _get(d: dict, *names: str):
    """Case-insensitive key lookup; returns the first name present."""
    lowered = {k.lower(): v for k, v in d.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _resolve_tz(name, default: tzinfo) -> tzinfo:
    if isinstance(name, dict):
        name = _get(name, "Id", "TimeZoneId", "Name")
    if not isinstance(name, str) or not name:
        return default
    name = _WINDOWS_TZ.get(name, name)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return default


def _parse_iso(s: str) -> datetime:
    # .NET emits 7 fractional digits; Python accepts at most 6.
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_time(value, default_tz: tzinfo = DEFAULT_TZ) -> datetime | None:
    """Parse an OWA time value into an aware UTC datetime."""
    if isinstance(value, str):
        tz = default_tz
        raw = value
    elif isinstance(value, dict):
        raw = _get(value, "DateTime")
        tz = _resolve_tz(_get(value, "TimeZone"), default_tz)
        if not isinstance(raw, str):
            return None
    else:
        return None
    try:
        dt = _parse_iso(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def _event_id(d: dict) -> str | None:
    item_id = _get(d, "ItemId")
    if isinstance(item_id, dict):
        item_id = _get(item_id, "Id")
    if isinstance(item_id, str) and item_id:
        return item_id
    rest_id = _get(d, "Id")
    return rest_id if isinstance(rest_id, str) and rest_id else None


def _to_event(d: dict, default_tz: tzinfo) -> Event | None:
    start_raw, end_raw = _get(d, "Start"), _get(d, "End")
    if start_raw is None or end_raw is None:
        return None
    source_id = _event_id(d)
    if source_id is None:
        return None
    start, end = parse_time(start_raw, default_tz), parse_time(end_raw, default_tz)
    if start is None or end is None:
        return None
    location = _get(d, "Location")
    if isinstance(location, dict):
        location = _get(location, "DisplayName")
    categories = _get(d, "Categories")
    return Event(
        source_id=source_id,
        subject=_get(d, "Subject") or "",
        start=start,
        end=end,
        is_all_day=bool(_get(d, "IsAllDayEvent", "IsAllDay")),
        location=location if isinstance(location, str) else "",
        show_as=_get(d, "FreeBusyType", "ShowAs", "LegacyFreeBusyStatus") or "Busy",
        is_cancelled=bool(_get(d, "IsCancelled")),
        categories=tuple(c for c in categories or () if isinstance(c, str)),
    )


def extract_events(payload, default_tz: tzinfo = DEFAULT_TZ) -> Iterator[Event]:
    """Walk an arbitrary JSON payload and yield every event-shaped object in it."""
    if isinstance(payload, dict):
        try:
            event = _to_event(payload, default_tz)
        except (TypeError, ValueError, AttributeError) as e:
            print(f"warning: skipped unparsable object: {e!r}", file=sys.stderr)
            event = None
        if event is not None:
            yield event
            return
        for v in payload.values():
            yield from extract_events(v, default_tz)
    elif isinstance(payload, list):
        for v in payload:
            yield from extract_events(v, default_tz)


def window(start: date, days: int, default_tz: tzinfo = DEFAULT_TZ) -> tuple[datetime, datetime]:
    """[start, start + days) as UTC datetimes, days counted in default_tz."""
    lo = datetime.combine(start, time(), default_tz).astimezone(timezone.utc)
    return lo, lo + timedelta(days=days)


def select_events(
    events, start: date, days: int, default_tz: tzinfo = DEFAULT_TZ
) -> list[Event]:
    """Dedupe by source_id and keep events overlapping [start, start + days)."""
    lo, hi = window(start, days, default_tz)
    seen: dict[str, Event] = {}
    for e in events:
        if e.end > lo and e.start < hi:
            seen[e.source_id] = e
    return sorted(seen.values(), key=lambda e: (e.start, e.subject))


def is_calendar_view(url: str) -> bool:
    """Only these responses list the user's events; others (suggestions, Copilot,
    scheduling pages) can hold event-shaped objects that must not be synced."""
    parsed = urlparse(url)
    actions = parse_qs(parsed.query).get("action", [])
    return "GetCalendarView" in actions or "calendarview" in parsed.path.lower()


def parse_dump_dir(dump_dir: Path, default_tz: tzinfo = DEFAULT_TZ) -> Iterator[Event]:
    for path in sorted(dump_dir.glob("*.json")):
        record = json.loads(path.read_text())
        if is_calendar_view(record["url"]):
            yield from extract_events(record["body"], default_tz)


# ---------------------------------------------------------------- browser


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


def _is_owa(url: str) -> bool:
    return _host(url).endswith(OWA_HOSTS)


def _check_session(page: Page) -> None:
    if _host(page.url).endswith(LOGIN_HOSTS):
        raise SessionExpired(f"Redirected to sign-in page: {_host(page.url)}")


def _dump_name(seq: int, url: str) -> str:
    parsed = urlparse(url)
    action = parse_qs(parsed.query).get("action", [None])[0]
    slug = action or parsed.path.rstrip("/").rsplit("/", 1)[-1] or "root"
    return f"{seq:04d}_{re.sub(r'[^A-Za-z0-9_.-]', '_', slug)[:60]}.json"


def login(ctx: BrowserContext, base_url: str = DEFAULT_BASE_URL) -> None:
    page = ctx.new_page()
    page.goto(f"{base_url}/calendar/", wait_until="commit")
    input("ブラウザでサインイン（MFA含む）し、予定表が表示されたら Enter を押してください: ")
    _check_session(page)
    page.close()


def connect(
    ctx: BrowserContext, base_url: str = DEFAULT_BASE_URL, timeout_ms: int = 30000
) -> OwaClient:
    """Open OWA once and build a client from what OWA itself sends while starting up:
    auth headers, the user's default calendar, and the calendars it displays."""
    requests: list[Request] = []
    startup: list[Response] = []

    def on_request(req: Request) -> None:
        if _is_owa(req.url) and "/owa/service.svc" in req.url:
            requests.append(req)

    def on_response(r: Response) -> None:
        if _is_owa(r.url) and "startupdata" in r.url:
            startup.append(r)

    # On the context, not the page: OWA fetches some of this from a service worker.
    ctx.on("request", on_request)
    ctx.on("response", on_response)
    page = ctx.new_page()
    client: OwaClient | None = None
    calendar_id: str | None = None
    shown: list[str] = []

    def drain() -> None:
        nonlocal client, calendar_id
        while startup:
            try:
                calendar_id = calendar_id or default_calendar_id(startup.pop(0).json())
            except Exception:
                pass
        while requests:
            req = requests.pop(0)
            headers = req.all_headers()
            if "action=GetCalendarView" in req.url:
                cid = view_calendar_id(headers.get("x-owa-urlpostdata"))
                if cid and cid not in shown:
                    shown.append(cid)
            if client is None:
                try:
                    client = OwaClient.from_request_headers(ctx, req.url, headers)
                except OwaApiError:
                    pass

    try:
        page.goto(f"{base_url}/calendar/", wait_until="commit")
        for _ in range(timeout_ms // 250):
            page.wait_for_timeout(250)
            _check_session(page)
            drain()
            if client and calendar_id and shown:
                break
        # OWA asks for every displayed calendar right after the first; collect those too.
        for _ in range(8):
            page.wait_for_timeout(250)
            drain()
    finally:
        ctx.remove_listener("request", on_request)
        ctx.remove_listener("response", on_response)
        page.close()
    if client is None:
        raise FetchIncomplete("OWA の通信から認証ヘッダーを取得できませんでした")
    if calendar_id is None:
        raise FetchIncomplete("既定の予定表を特定できませんでした")
    client.folder_id = calendar_id
    client.shown_calendar_ids = tuple(dict.fromkeys([calendar_id, *shown]))
    return client


def read_calendar(
    client: OwaClient,
    folder_id: str,
    start: date,
    days: int,
    dump_dir: Path | None = None,
    default_tz: tzinfo = DEFAULT_TZ,
    chunk_days: int = 14,
) -> list[Event]:
    """Events of one calendar overlapping [start, start + days), read in chunks."""
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)
    lo, hi = window(start, days, default_tz)
    events: list[Event] = []
    a = lo
    while a < hi:
        b = min(a + timedelta(days=chunk_days), hi)
        try:
            body = client.calendar_view(folder_id, a, b, default_tz)
        except OwaApiError as e:
            # A missing range would look like deleted events to the sync, so fail loudly.
            raise FetchIncomplete(str(e)) from e
        if dump_dir:
            name = f"{a.astimezone(default_tz):%Y%m%d}_{folder_id[-8:]}_GetCalendarView.json"
            record = {"url": "service.svc?action=GetCalendarView", "body": body}
            (dump_dir / re.sub(r"[^A-Za-z0-9_.-]", "_", name)).write_text(
                json.dumps(record, ensure_ascii=False, indent=2)
            )
        events.extend(extract_events(body, default_tz))
        a = b
    return select_events(events, start, days, default_tz)


def fetch(
    ctx: BrowserContext,
    start: date,
    days: int = 7,
    base_url: str = DEFAULT_BASE_URL,
    dump_dir: Path | None = None,
    default_tz: tzinfo = DEFAULT_TZ,
) -> tuple[list[Event], OwaClient]:
    """Events of the user's default calendar, plus the client for further calls."""
    client = connect(ctx, base_url)
    assert client.folder_id is not None
    return read_calendar(client, client.folder_id, start, days, dump_dir, default_tz), client


def fetch_events(ctx: BrowserContext, start: date, days: int = 7, **kwargs) -> list[Event]:
    return fetch(ctx, start, days, **kwargs)[0]


# Header values that grant access; recorded as "<redacted>" so dumps hold no secrets.
_SECRET_HEADERS = {"authorization", "cookie", "x-owa-canary", "x-owa-sessionid"}


def _wait_for_enter(page: Page, prompt: str, on_tick: Callable[[], None] = lambda: None) -> None:
    """input(), but keep Playwright's event loop running meanwhile.

    The sync API only delivers events (requests, responses) while Python is inside a
    Playwright call, so a bare input() would make every handler miss its events.
    """
    done = threading.Event()

    def wait() -> None:
        try:
            input(prompt)
        except EOFError:
            pass
        finally:
            done.set()

    threading.Thread(target=wait, daemon=True).start()
    while not done.is_set():
        page.wait_for_timeout(250)
        on_tick()


def record(ctx: BrowserContext, out_dir: Path, base_url: str = DEFAULT_BASE_URL) -> int:
    """Record the POST requests OWA sends while the user edits events by hand.

    Used to learn the exact CreateItem / UpdateItem / DeleteItem payloads of this
    tenant before ogsync sends them itself.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    pending: list[Request] = []

    def on_finished(req: Request) -> None:
        if req.method == "POST" and _is_owa(req.url):
            pending.append(req)

    # On the context, not the page, so popup windows (e.g. a full event form) count too.
    ctx.on("requestfinished", on_finished)
    page = ctx.new_page()
    page.goto(f"{base_url}/calendar/", wait_until="commit")
    saved = 0

    def drain() -> None:
        # Save as they arrive: response bodies may be evicted if read much later.
        nonlocal saved
        while pending:
            req = pending.pop(0)
            _save_request(req, out_dir / _dump_name(saved, req.url))
            saved += 1

    _wait_for_enter(
        page,
        "ブラウザで「ogsync test」という予定を 作成 → 時間を変更 → 削除 してください。\n"
        "終わったら Enter を押してください: ",
        drain,
    )
    page.wait_for_timeout(1000)
    drain()
    ctx.remove_listener("requestfinished", on_finished)
    _check_session(page)
    page.close()
    return saved


def _save_request(req: Request, path: Path) -> None:
    headers = {
        k: "<redacted>" if k.lower() in _SECRET_HEADERS else v
        for k, v in req.all_headers().items()
    }
    status = None
    try:
        resp = req.response()
        status = resp.status if resp else None
        body = resp.json() if resp else None
    except Exception:
        body = None
    entry = {
        "url": req.url,
        "method": req.method,
        "headers": headers,
        "request": req.post_data,
        "status": status,
        "body": body,
    }
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2))
