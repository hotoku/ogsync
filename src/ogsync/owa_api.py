"""Read, create and delete Outlook events through OWA's own service.svc endpoint.

Payloads mirror what OWA sends when a user views and edits events by hand
(recorded with `ogsync record`). Authentication is borrowed from a live request OWA made in the
same browser session, so no token is stored anywhere.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, tzinfo
from urllib.parse import quote, unquote, urlparse

from playwright.sync_api import BrowserContext

OWA_TZ_ID = "Tokyo Standard Time"  # Windows name of Asia/Tokyo, as OWA sends it

# Headers copied from a live OWA request. Everything else (pseudo headers, cookies
# handled by the context, per-request tracing ids) is left out.
_AUTH_HEADERS = (
    "authorization",
    "x-anchormailbox",
    "x-owa-sessionid",
    "x-owa-canary",
    "x-client-version",
    "x-owa-hosted-ux",
    "prefer",
)


class OwaApiError(Exception):
    pass


def default_calendar_id(startup_body) -> str | None:
    """The user's own calendar folder, from OWA's startupdata response."""
    try:
        settings = startup_body["owaUserConfig"]["SessionSettings"]
        index = settings["DefaultFolderNames"].index("calendar")
        folder = settings["DefaultFolderIds"][index]
    except (KeyError, ValueError, IndexError, TypeError):
        return None
    folder_id = folder.get("Id") if isinstance(folder, dict) else folder
    return folder_id if isinstance(folder_id, str) and folder_id else None


def view_calendar_id(urlpostdata: str | None) -> str | None:
    """The calendar a GetCalendarView request asked for (payload is in a header)."""
    try:
        payload = json.loads(unquote(urlpostdata or ""))
        return payload["Body"]["CalendarId"]["BaseFolderId"]["Id"]
    except (ValueError, KeyError, TypeError):
        return None


def _header() -> dict:
    return {
        "__type": "JsonRequestHeaders:#Exchange",
        "RequestServerVersion": "V2018_01_08",
        "TimeZoneContext": {
            "__type": "TimeZoneContext:#Exchange",
            "TimeZoneDefinition": {"__type": "TimeZoneDefinitionType:#Exchange", "Id": OWA_TZ_ID},
        },
    }


def _local(dt: datetime, tz: tzinfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%dT%H:%M:%S.000")


def create_payload(
    subject: str,
    start: datetime,
    end: datetime,
    category: str,
    tz: tzinfo,
    folder_id: str | None = None,
) -> dict:
    """CreateCalendarEvent as OWA sends it for a new event without attendees.

    Copied from a recorded request: only times, ids, subject, reminder and category
    differ. Null fields of the recording are omitted, but empty lists and objects
    are kept, since the server may not treat them as optional.
    """
    local_id = f"localevent:{uuid.uuid4()}"
    item = {
        "__type": "CalendarItem:#Exchange",
        "AppendOnSend": [],
        "AssociatedTasks": [],
        "Attachments": [],
        "Body": {"BodyType": "HTML", "Value": ""},
        "CalendarEventClassifications": [],
        "Categories": [category],
        "CharmId": 0,
        "ClientSeriesId": local_id,
        "DoNotForwardMeeting": False,
        "DocLinks": [],
        "EffectiveRights": {"Delete": True, "Modify": True, "Read": True, "ViewPrivateItems": True},
        "End": _local(end, tz),
        "EndTimeZoneId": OWA_TZ_ID,
        "ExtendedProperty": [],
        "FreeBusyType": "Busy",
        "HideAttendees": False,
        "InboxReminders": [],
        "IsAllDayEvent": False,
        "IsBookedFreeBlocks": False,
        "IsDraft": False,
        "IsExternalSender": False,
        "IsFollowableMeeting": False,
        "IsOrganizer": True,
        "IsPresenter": False,
        "IsResponseRequested": True,
        "IsRoomRequested": False,
        "ItemId": {"__type": "ItemId:#Exchange", "Id": local_id},
        "Locations": [],
        "MentionsEx": [],
        "MuteNotifications": False,
        "OnlineMeeting": {},
        "PrependOnSend": [],
        "ReminderIsSet": False,
        "ReminderMinutesBeforeStart": 0,
        "RequestedAttendanceMode": "Default",
        "Resources": [],
        "Sensitivity": "Normal",
        "Start": _local(start, tz),
        "StartTimeZoneId": OWA_TZ_ID,
        "Subject": subject,
        "id": local_id,
    }
    body = {
        "__type": "CreateItemRequest:#Exchange",
        "Items": [item],
        "ClientSupportsIrm": True,
        "UnpromotedInlineImageCount": 0,
        "ItemShape": {"__type": "ItemResponseShape:#Exchange", "BaseShape": "IdOnly"},
        # Never send invitations, even though the item has no attendees.
        "SendMeetingInvitations": "SendToNone",
        "OutboundCharset": "AutoDetect",
        "UseGB18030": False,
        "UseISO885915": False,
    }
    if folder_id:
        item["ParentFolderId"] = {"Id": folder_id}
        body["SavedItemFolderId"] = {
            "__type": "TargetFolderId:#Exchange",
            "BaseFolderId": {"__type": "FolderId:#Exchange", "Id": folder_id},
        }
    return {"__type": "CreateItemJsonRequest:#Exchange", "Header": _header(), "Body": body}


def calendar_view_payload(folder_id: str, start: datetime, end: datetime, tz: tzinfo) -> dict:
    return {
        "__type": "GetCalendarViewJsonRequest:#Exchange",
        "Header": _header(),
        "Body": {
            "__type": "GetCalendarViewRequest:#Exchange",
            "CalendarId": {
                "__type": "TargetFolderId:#Exchange",
                "BaseFolderId": {"__type": "FolderId:#Exchange", "Id": folder_id},
            },
            "RangeStart": _local(start, tz),
            "RangeEnd": _local(end, tz),
            "ClientSupportsIrm": True,
            "OptimizeExtendedPropertyLoading": True,
        },
    }


def delete_payload(item_id: str) -> dict:
    return {
        "__type": "DeleteCalendarEventJsonRequest:#Exchange",
        "Header": _header(),
        "Body": {
            "__type": "DeleteCalendarEventRequest:#Exchange",
            "EventId": {"__type": "ItemId:#Exchange", "Id": item_id},
            "EventScope": 1,
        },
    }


@dataclass
class OwaClient:
    ctx: BrowserContext
    origin: str  # e.g. https://outlook.cloud.microsoft; OWA may redirect between domains
    auth: dict[str, str]
    folder_id: str | None = None  # the user's default calendar
    # Calendars OWA itself displayed (default plus any shared or extra ones the user
    # opened). Only used to look for stray ogsync events.
    shown_calendar_ids: tuple[str, ...] = ()

    @classmethod
    def from_request_headers(cls, ctx: BrowserContext, url: str, headers: dict[str, str]) -> "OwaClient":
        parsed = urlparse(url)
        auth = {k: v for k, v in headers.items() if k.lower() in _AUTH_HEADERS}
        if "authorization" not in {k.lower() for k in auth}:
            raise OwaApiError("OWA の通信から認証ヘッダーを取得できませんでした")
        return cls(ctx, f"{parsed.scheme}://{parsed.netloc}", auth)

    def calendar_view(self, folder_id: str, start: datetime, end: datetime, tz: tzinfo) -> dict:
        """Raw GetCalendarView response for [start, end) of one calendar."""
        body = self._post(
            "GetCalendarView", calendar_view_payload(folder_id, start, end, tz), in_header=True
        )
        result = body.get("Body") if isinstance(body, dict) else None
        if not isinstance(result, dict) or result.get("ResponseClass") != "Success":
            raise OwaApiError(f"GetCalendarView: {_describe(result or {})}")
        return body

    def _post(self, action: str, payload: dict, in_header: bool = False) -> dict:
        headers = {
            **self.auth,
            "action": action,
            "x-owa-actionsource": action,
            "x-req-source": "Calendar",
            "content-type": "application/json; charset=utf-8",
        }
        data = json.dumps(payload, ensure_ascii=False)
        if in_header:
            # OWA sends some actions with an empty body and the payload in this header.
            headers["x-owa-urlpostdata"] = quote(data)
            data = ""
        resp = self.ctx.request.post(
            f"{self.origin}/owa/service.svc?action={action}&app=Calendar",
            headers=headers,
            data=data,
        )
        if not resp.ok:
            raise OwaApiError(f"{action}: HTTP {resp.status}")
        return resp.json()

    def create_event(self, subject: str, start: datetime, end: datetime, category: str, tz: tzinfo) -> str:
        payload = create_payload(subject, start, end, category, tz, self.folder_id)
        body = self._post("CreateCalendarEvent", payload)
        try:
            message = body["Body"]["ResponseMessages"]["Items"][0]
            if message["ResponseClass"] != "Success":
                raise OwaApiError(f"CreateCalendarEvent: {_describe(message)}")
            return message["Items"][0]["ItemId"]["Id"]
        except (KeyError, IndexError, TypeError) as e:
            raise OwaApiError(f"CreateCalendarEvent: 想定外の応答 {e!r}") from e

    def delete_event(self, item_id: str) -> None:
        body = self._post("DeleteCalendarEvent", delete_payload(item_id), in_header=True)
        result = body.get("Body") or {}
        if result.get("ResponseClass") != "Success":
            raise OwaApiError(f"DeleteCalendarEvent: {_describe(result)}")


def _describe(message: dict) -> str:
    return " / ".join(str(message[k]) for k in ("ResponseCode", "MessageText") if message.get(k))
