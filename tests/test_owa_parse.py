from datetime import date, datetime, timedelta, timezone

import pytest

from ogsync import owa


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


EWS_STYLE = {
    "Body": {
        "ResponseClass": "Success",
        "Items": [
            {
                "__type": "CalendarItem:#Exchange",
                "ItemId": {"Id": "AAMk-1", "ChangeKey": "x"},
                "Subject": "定例",
                "Start": "2026-10-23T10:00:00+09:00",
                "End": "2026-10-23T11:00:00+09:00",
                "IsAllDayEvent": False,
                "FreeBusyType": "Busy",
                "Location": {"DisplayName": "会議室A"},
            },
            {
                "ItemId": {"Id": "AAMk-2"},
                "Subject": "休暇",
                "Start": "2026-10-24T00:00:00+09:00",
                "End": "2026-10-25T00:00:00+09:00",
                "IsAllDayEvent": True,
                "FreeBusyType": "OOF",
                "IsCancelled": True,
            },
        ],
    }
}

REST_STYLE = {
    "value": [
        {
            "id": "rest-1",
            "subject": "1on1",
            "start": {"dateTime": "2026-10-23T05:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-10-23T05:30:00.0000000", "timeZone": "UTC"},
            "showAs": "tentative",
            "location": {"displayName": ""},
        },
        {
            "Id": "rest-2",
            "Subject": "朝会",
            "Start": {"DateTime": "2026-10-23T09:00:00", "TimeZone": "Tokyo Standard Time"},
            "End": {"DateTime": "2026-10-23T09:15:00", "TimeZone": "Tokyo Standard Time"},
        },
    ]
}


def test_ews_style():
    events = list(owa.extract_events(EWS_STYLE))
    assert [e.source_id for e in events] == ["AAMk-1", "AAMk-2"]
    first, second = events
    assert (first.start, first.end) == (utc(2026, 10, 23, 1), utc(2026, 10, 23, 2))
    assert first.location == "会議室A"
    assert second.is_all_day and second.is_cancelled and second.show_as == "OOF"


def test_rest_style():
    events = list(owa.extract_events(REST_STYLE))
    assert [e.source_id for e in events] == ["rest-1", "rest-2"]
    assert events[0].start == utc(2026, 10, 23, 5)
    assert events[0].show_as == "tentative"
    assert events[1].start == utc(2026, 10, 23, 0)


def test_ignores_objects_without_id():
    payload = {"range": {"Start": "2026-10-23T00:00:00Z", "End": "2026-10-24T00:00:00Z"}}
    assert list(owa.extract_events(payload)) == []


def test_select_events_dedupes_and_filters():
    events = list(owa.extract_events(EWS_STYLE)) * 2
    selected = owa.select_events(events, date(2026, 10, 23), days=1)
    assert [e.source_id for e in selected] == ["AAMk-1"]


def test_timezone_as_object():
    payload = {
        "Id": "tz-obj",
        "Start": {"DateTime": "2026-10-23T09:00:00", "TimeZone": {"Id": "Tokyo Standard Time"}},
        "End": {"DateTime": "2026-10-23T10:00:00", "TimeZone": {"Id": "Tokyo Standard Time"}},
    }
    [event] = owa.extract_events(payload)
    assert event.start == utc(2026, 10, 23, 0)


def test_is_calendar_view():
    assert owa.is_calendar_view(
        "https://outlook.office.com/owa/service.svc?action=GetCalendarView&app=Calendar"
    )
    assert not owa.is_calendar_view("https://outlook.office.com/owa/service.svc?action=GetTimeZone")



class FakeClient:
    def __init__(self, items_by_call):
        self.items_by_call = list(items_by_call)
        self.ranges = []

    def calendar_view(self, folder_id, start, end, tz):
        self.ranges.append((folder_id, start, end))
        return {"Body": {"ResponseClass": "Success", "Items": self.items_by_call.pop(0)}}


def item(item_id: str, day: int) -> dict:
    return {
        "ItemId": {"Id": item_id},
        "Subject": "s",
        "Start": f"2026-10-{day:02d}T10:00:00+09:00",
        "End": f"2026-10-{day:02d}T11:00:00+09:00",
    }


def test_read_calendar_reads_in_chunks_and_dedupes():
    client = FakeClient([[item("a", 20), item("b", 30)], [item("b", 30), item("c", 31)]])
    events = owa.read_calendar(client, "cal", date(2026, 10, 19), 20, chunk_days=14)
    assert [e.source_id for e in events] == ["a", "b", "c"]
    (f1, s1, e1), (f2, s2, e2) = client.ranges
    assert f1 == f2 == "cal" and e1 == s2 and e2 - s1 == timedelta(days=20)


def test_read_calendar_failure_is_fetch_incomplete():
    class Failing:
        def calendar_view(self, *a):
            raise owa.OwaApiError("GetCalendarView: ErrorX")

    with pytest.raises(owa.FetchIncomplete):
        owa.read_calendar(Failing(), "cal", date(2026, 10, 19), 7)
