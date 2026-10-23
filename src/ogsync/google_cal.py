"""Google Calendar access to the user's primary calendar."""

from datetime import datetime, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from ogsync import config

# Read and write events (not calendar settings or sharing). Writes are limited in
# code to events tagged ogsync_source=outlook.
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CALENDAR_ID = "primary"
SOURCE_OUTLOOK = "outlook"


class NotAuthorized(Exception):
    pass


def authorize() -> None:
    if not config.CLIENT_SECRET.exists():
        raise NotAuthorized(f"OAuth クライアントの JSON を {config.CLIENT_SECRET} に置いてください")
    flow = InstalledAppFlow.from_client_secrets_file(str(config.CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=False)
    _save(creds)


def _save(creds: Credentials) -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.GOOGLE_TOKEN.write_text(creds.to_json())
    config.GOOGLE_TOKEN.chmod(0o600)


def service():
    if not config.GOOGLE_TOKEN.exists():
        raise NotAuthorized("Google が未認可です")
    # Load with the scopes saved at authorization time; passing SCOPES would mask them.
    creds = Credentials.from_authorized_user_file(str(config.GOOGLE_TOKEN))
    if not set(SCOPES) <= set(creds.scopes or ()):
        raise NotAuthorized("必要な権限が変わりました。再認可が必要です")
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise NotAuthorized("Google のトークンが無効です")
        creds.refresh(Request())
        _save(creds)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _list(svc, lo: datetime | None = None, hi: datetime | None = None, **filters) -> list[dict]:
    if lo is not None:
        filters["timeMin"] = lo.isoformat()
    if hi is not None:
        filters["timeMax"] = hi.isoformat()
    items: list[dict] = []
    token = None
    while True:
        resp = (
            svc.events()
            .list(
                calendarId=CALENDAR_ID,
                singleEvents=True,
                maxResults=250,
                pageToken=token,
                **filters,
            )
            .execute()
        )
        items.extend(resp.get("items", []))
        if not (token := resp.get("nextPageToken")):
            return items


def list_synced(svc, lo: datetime, hi: datetime) -> list[dict]:
    """Copies of Outlook events made by this tool that overlap [lo, hi)."""
    return _list(svc, lo, hi, privateExtendedProperty=f"ogsync_source={SOURCE_OUTLOOK}")


def list_all(svc, lo: datetime, hi: datetime) -> list[dict]:
    """Every event overlapping [lo, hi), widened by a day on each side.

    The margin lets overlapping events that cross the window edge be merged into
    the same block every run; otherwise a block's key would shift as the window slides.
    """
    margin = timedelta(days=1)
    return _list(svc, lo - margin, hi + margin)


def list_created(svc) -> list[dict]:
    """Every event this tool created in primary, regardless of date."""
    return _list(svc, privateExtendedProperty=f"ogsync_source={SOURCE_OUTLOOK}")


def delete(svc, google_id: str) -> None:
    svc.events().delete(calendarId=CALENDAR_ID, eventId=google_id).execute()
