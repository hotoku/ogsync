from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Event:
    """A calendar event normalized to UTC, independent of the source calendar."""

    source_id: str
    subject: str
    start: datetime
    end: datetime
    is_all_day: bool = False
    location: str = ""
    show_as: str = "Busy"  # Free / Tentative / Busy / OOF / WorkingElsewhere
    is_cancelled: bool = False
    categories: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "subject": self.subject,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "is_all_day": self.is_all_day,
            "location": self.location,
            "show_as": self.show_as,
            "is_cancelled": self.is_cancelled,
            "categories": list(self.categories),
        }
