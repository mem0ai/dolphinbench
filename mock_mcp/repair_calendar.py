"""Candidate calendar tools with explicit dates and stored outcomes."""

from __future__ import annotations

from datetime import date as calendar_date, datetime, time, timedelta
import re
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mock_mcp.repair_support import CandidateStore, email_address, nonblank


def parse_time(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
        r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})", value
    ):
        raise ValueError("Use a date and time with T and an explicit timezone: Z or +HH:MM / -HH:MM.")
    # datetime accepts overflowing offset minutes, which are not valid HH:MM offsets.
    if not value.endswith("Z") and int(value[-2:]) > 59:
        raise ValueError("Timezone offset minutes must be between 00 and 59.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("The date, time, or timezone offset is invalid.") from exc


def validate_event(event: dict) -> tuple[datetime, datetime]:
    nonblank(event.get("title"), "title")
    start, end = parse_time(event.get("start")), parse_time(event.get("end"))
    if end <= start:
        raise ValueError("end must be later than start.")
    return start, end


def attendee_addresses(attendees: list[str] | None) -> list[str]:
    if attendees is None:
        return []
    if not isinstance(attendees, list):
        raise ValueError("attendees must be a list of email addresses.")
    return list(dict.fromkeys(email_address(value) for value in attendees))


class CandidateCalendar:
    def __init__(self, store: CandidateStore):
        self.store = store

    def list_calendar_events(self, date: str, timezone: str) -> str:
        """List events overlapping one day in the timezone you specify.

        Args:
            date: Calendar date, YYYY-MM-DD.
            timezone: IANA timezone name, such as America/Los_Angeles or UTC.
                The same timezone is used for every event in this request.

        Returns:
            A list containing each event's id, title, start, end, attendees,
            and any body or location. An event ending exactly at the start of
            this day is excluded. Invalid dates, timezones, or stored event
            times return ok=false and an error, not an empty calendar.
        """
        args = {"date": date, "timezone": timezone}
        try:
            if not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date):
                raise ValueError("date must be YYYY-MM-DD.")
            day = calendar_date.fromisoformat(date)
            zone = ZoneInfo(nonblank(timezone, "timezone"))
            lower = datetime.combine(day, time.min, zone)
            upper = datetime.combine(day + timedelta(days=1), time.min, zone)
        except (ValueError, ZoneInfoNotFoundError, OverflowError) as exc:
            return self.store.error("list_calendar_events", args, str(exc))
        with self.store.lock:
            state = self.store.read()
            events = []
            for event in state.get("calendar", []):
                try:
                    if not isinstance(event, dict):
                        raise ValueError("Each stored event must be an object.")
                    start, end = parse_time(event.get("start")), parse_time(event.get("end"))
                    if end <= start:
                        raise ValueError("end must be later than start.")
                except ValueError as exc:
                    return self.store.error("list_calendar_events", args, f"A stored event has invalid times: {exc}")
                if start < upper and end > lower:
                    visible = {key: event.get(key) for key in ("title", "start", "end", "attendees")}
                    visible["id"] = event.get("id") or event.get("event_id")
                    for key in ("body", "location"):
                        if key in event:
                            visible[key] = event[key]
                    events.append(visible)
            return self.store.reply("list_calendar_events", args, events)

    def _save_event(self, tool: str, args: dict, event_id: str | None, changes: dict) -> str:
        with self.store.lock:
            state = self.store.read()
            calendar = state.setdefault("calendar", [])
            if not isinstance(calendar, list) or any(not isinstance(event, dict) for event in calendar):
                return self.store.error(tool, args, "The stored calendar must be a list of events.")
            target = None
            if event_id is not None:
                matches = [event for event in calendar if (event.get("id") or event.get("event_id")) == event_id]
                if len(matches) != 1:
                    return self.store.error(tool, args, "event_id must identify exactly one existing event. Use list_calendar_events.")
                target = matches[0]
            event = dict(target) if target is not None else {"id": f"evt_{uuid4().hex}", "attendees": []}
            event.update(changes)
            try:
                start, end = validate_event(event)
                attendees = attendee_addresses(event.get("attendees"))
                if "body" in event and event["body"] is not None and not isinstance(event["body"], str):
                    raise ValueError("body must be text.")
                for existing in calendar:
                    if existing is target:
                        continue
                    old_start, old_end = parse_time(existing.get("start")), parse_time(existing.get("end"))
                    if (old_start == start and old_end == end
                            and set(attendees).intersection(attendee_addresses(existing.get("attendees")))):
                        raise ValueError("An event with the same start and end already includes one of these attendees.")
            except ValueError as exc:
                return self.store.error(tool, args, str(exc))
            if target is None:
                calendar.append(event)
            else:
                target.clear()
                target.update(event)
            self.store.backend._save_state(state)
            return self.store.reply(tool, args, {"ok": True, "id": event.get("id") or event.get("event_id"),
                                                "event": event})

    def create_calendar_event(self, title: str, start: str, end: str,
                              attendees: list[str] | None = None, body: str | None = None) -> str:
        """Create an event, meeting, or hold in the calendar.

        Args:
            title: Nonempty event title.
            start: YYYY-MM-DDTHH:MM, optionally with seconds, followed by Z
                or an explicit +HH:MM / -HH:MM timezone offset.
            end: Same format as start; must be later than start.
            attendees: Email addresses only, not names. Omit for a hold with
                no invitees. Use list_contacts when you need an address.
            body: Optional event description.

        Returns:
            ok=true and the stored event, including its unique id. An event
            with identical start/end and a shared attendee is rejected.
            Invalid input returns ok=false and an error without adding an event.
        """
        args = {"title": title, "start": start, "end": end, "attendees": attendees, "body": body}
        try:
            changes = {"title": title, "start": start, "end": end, "attendees": attendee_addresses(attendees)}
        except ValueError as exc:
            return self.store.error("create_calendar_event", args, str(exc))
        if body is not None:
            changes["body"] = body
        return self._save_event("create_calendar_event", args, None, changes)

    def update_calendar_event(self, event_id: str, start: str | None = None, end: str | None = None,
                              title: str | None = None, attendees: list[str] | None = None,
                              body: str | None = None) -> str:
        """Change an existing calendar event without replacing its id.

        Args:
            event_id: Existing id from list_calendar_events.
            start: Optional new YYYY-MM-DDTHH:MM, optionally with seconds,
                followed by Z or an explicit +HH:MM / -HH:MM offset.
            end: Optional new time in the same format. The resulting end
                must be later than the resulting start, even if only one changes.
            title: Optional nonempty replacement title.
            attendees: Optional replacement list of email addresses. Use []
                to remove all invitees. Use list_contacts to find addresses.
            body: Optional replacement description. Use an empty string to clear it.

        Returns:
            ok=true and the stored event. Omitted or null fields stay unchanged.
            Supply at least one field to change.
            A duplicate time range with a shared attendee is rejected, as on
            creation. Missing ids or invalid values return ok=false and an error;
            the original event stays unchanged.
        """
        args = {"event_id": event_id, "start": start, "end": end,
                "title": title, "attendees": attendees, "body": body}
        try:
            nonblank(event_id, "event_id")
            changes = {key: value for key, value in args.items() if key != "event_id" and value is not None}
            if not changes:
                raise ValueError("Supply at least one field to change.")
            if attendees is not None:
                changes["attendees"] = attendee_addresses(attendees)
        except ValueError as exc:
            return self.store.error("update_calendar_event", args, str(exc))
        return self._save_event("update_calendar_event", args, event_id, changes)
