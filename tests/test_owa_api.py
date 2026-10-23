import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from ogsync import owa_api
from ogsync.owa import DEFAULT_TZ


class FakeRequest:
    def __init__(self, response: dict):
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, headers, data):
        self.calls.append({"url": url, "headers": headers, "data": data})
        return SimpleNamespace(ok=True, status=200, json=lambda: self.response)


def client(response: dict) -> tuple[owa_api.OwaClient, FakeRequest]:
    fake = FakeRequest(response)
    c = owa_api.OwaClient.from_request_headers(
        SimpleNamespace(request=fake),
        "https://outlook.cloud.microsoft/owa/service.svc?action=GetCalendarView&app=Calendar",
        {
            ":path": "/owa/service.svc",
            "authorization": "Bearer t",
            "cookie": "c=1",
            "x-anchormailbox": "PUID:x",
            "traceparent": "00-abc",
        },
    )
    return c, fake


START = datetime(2026, 10, 24, 0, tzinfo=timezone.utc)  # 09:00 JST
END = datetime(2026, 10, 24, 1, tzinfo=timezone.utc)


def test_auth_headers_are_filtered():
    c, _ = client({})
    assert c.origin == "https://outlook.cloud.microsoft"
    assert c.auth == {"authorization": "Bearer t", "x-anchormailbox": "PUID:x"}


def test_missing_authorization_is_rejected():
    with pytest.raises(owa_api.OwaApiError):
        owa_api.OwaClient.from_request_headers(SimpleNamespace(), "https://o/x", {"cookie": "c"})


def test_create_sends_local_times_and_never_invites():
    ok = {
        "Body": {
            "ResponseMessages": {
                "Items": [{"ResponseClass": "Success", "Items": [{"ItemId": {"Id": "new-id"}}]}]
            }
        }
    }
    c, fake = client(ok)
    assert c.create_event("BLOCKED", START, END, "ogsync", DEFAULT_TZ) == "new-id"
    [call] = fake.calls
    assert call["url"].startswith("https://outlook.cloud.microsoft/owa/service.svc?action=CreateCalendarEvent")
    assert call["headers"]["action"] == "CreateCalendarEvent"
    body = json.loads(call["data"])["Body"]
    [item] = body["Items"]
    assert body["SendMeetingInvitations"] == "SendToNone"
    assert (item["Start"], item["End"]) == ("2026-10-24T09:00:00.000", "2026-10-24T10:00:00.000")
    assert item["StartTimeZoneId"] == "Tokyo Standard Time"
    assert item["Categories"] == ["ogsync"] and item["ReminderIsSet"] is False
    assert "RequiredAttendees" not in item and "OptionalAttendees" not in item


def test_create_failure_is_raised():
    c, _ = client({"Body": {"ResponseMessages": {"Items": [{"ResponseClass": "Error", "ResponseCode": "X"}]}}})
    with pytest.raises(owa_api.OwaApiError):
        c.create_event("BLOCKED", START, END, "ogsync", DEFAULT_TZ)


def test_delete_puts_payload_in_header_like_owa():
    c, fake = client({"Body": {"ResponseClass": "Success"}})
    c.delete_event("item-1")
    [call] = fake.calls
    assert call["data"] == ""
    payload = json.loads(unquote(call["headers"]["x-owa-urlpostdata"]))
    assert payload["Body"]["EventId"]["Id"] == "item-1"
    assert payload["__type"] == "DeleteCalendarEventJsonRequest:#Exchange"


def test_create_payload_matches_recorded_shape():
    payload = owa_api.create_payload("BLOCKED", START, END, "ogsync", DEFAULT_TZ, "folder-1")
    [item] = payload["Body"]["Items"]
    assert item["ItemId"]["Id"] == item["ClientSeriesId"] == item["id"]
    assert item["ItemId"]["Id"].startswith("localevent:")
    assert item["Attachments"] == [] and item["OnlineMeeting"] == {}
    assert item["ParentFolderId"] == {"Id": "folder-1"}
    assert payload["Body"]["SavedItemFolderId"]["BaseFolderId"]["Id"] == "folder-1"


def test_error_includes_message_text():
    c, _ = client({"Body": {"ResponseClass": "Error", "ResponseCode": "ErrorX", "MessageText": "why"}})
    with pytest.raises(owa_api.OwaApiError, match="ErrorX / why"):
        c.delete_event("item-1")


def test_default_calendar_id_from_startup_data():
    startup = {"owaUserConfig": {"SessionSettings": {
        "DefaultFolderNames": [None, "calendar", "contacts"],
        "DefaultFolderIds": [{"Id": "root"}, {"Id": "cal-id"}, {"Id": "contacts-id"}],
    }}}
    assert owa_api.default_calendar_id(startup) == "cal-id"
    assert owa_api.default_calendar_id({}) is None


def test_view_calendar_id_reads_urlpostdata():
    from urllib.parse import quote
    payload = owa_api.calendar_view_payload("cal-id", START, END, DEFAULT_TZ)
    assert owa_api.view_calendar_id(quote(json.dumps(payload))) == "cal-id"
    assert owa_api.view_calendar_id(None) is None


def test_calendar_view_sends_range_in_header():
    c, fake = client({"Body": {"ResponseClass": "Success", "Items": []}})
    c.calendar_view("cal-id", START, END, DEFAULT_TZ)
    [call] = fake.calls
    body = json.loads(unquote(call["headers"]["x-owa-urlpostdata"]))["Body"]
    assert (body["RangeStart"], body["RangeEnd"]) == ("2026-10-24T09:00:00.000", "2026-10-24T10:00:00.000")
    assert body["CalendarId"]["BaseFolderId"]["Id"] == "cal-id"
