"""Candidate travel tools that book supplied offers, not invented prices."""

from __future__ import annotations

import copy
import math
import re
from typing import Literal

from mock_mcp.repair_calendar import parse_time
from mock_mcp.repair_support import CandidateStore, amount_value, date_value, integer, nonblank, one_record


SEAT = Literal["aisle", "window", "middle", "any"]


def airport(value: str) -> str:
    code = nonblank(value, "Airport code").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ValueError("Use a three-letter IATA airport code.")
    return code


def date_range(start: str, end: str, *, same_day: bool = False) -> None:
    first, last = date_value(start), date_value(end)
    if last < first or (last == first and not same_day):
        raise ValueError("End date must follow start date." if not same_day else "Return date cannot precede outbound date.")


def distance(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Distance must be a finite nonnegative number of miles.")
    return value


class CandidateTravel:
    def __init__(self, store: CandidateStore):
        self.store = store

    def _offer(self, quote: dict, kind: str) -> None:
        nonblank(quote.get("id"), "Quote id")
        if quote.get("currency") != "USD":
            raise ValueError("The quote must provide its total price in USD.")
        amount_value(quote.get("total_price_usd"), allow_zero=True)
        if type(quote.get("available")) is not bool:
            raise ValueError("The quote must explicitly state whether it is available.")
        if quote.get("expires_at") is not None:
            parse_time(quote["expires_at"])
        if kind == "flight":
            if airport(quote.get("origin")) == airport(quote.get("destination")):
                raise ValueError("Origin and destination must differ.")
            date_range(quote.get("outbound_date"), quote.get("return_date"), same_day=True)
            nonblank(quote.get("airline"), "Quoted airline")
            if quote.get("seat_preference") not in ("aisle", "window", "middle", "any"):
                raise ValueError("Quoted seat_preference must be aisle, window, middle, or any.")
        else:
            nonblank(quote.get("hotel"), "Quoted hotel")
            nonblank(quote.get("location"), "Quoted location")
            date_range(quote.get("check_in"), quote.get("check_out"))
            integer(quote.get("guests"), "Quoted guests")

    def _available(self, quote: dict) -> bool:
        if not quote["available"]:
            return False
        return (quote.get("expires_at") is None
                or parse_time(quote["expires_at"]) > parse_time(self.store.backend._now_iso()))

    def list_flight_quotes(self, origin: str, destination: str, outbound_date: str, return_date: str,
                           airline: str | None = None, seat_preference: SEAT | None = None,
                           max_price_usd: float | None = None) -> str:
        """List supplied, available round-trip flight quotes for one traveler.

        origin and destination are three-letter airport codes; case and outer
        whitespace are ignored. Dates are YYYY-MM-DD; return may be the same day.
        Optional airline filters by name ignoring case; seat_preference is aisle,
        window, middle, or any. max_price_usd caps the total fare including fees;
        null means no cap, and zero means only free offers. No fare is invented.
        Returns matching quotes with ids, itineraries, and total_price_usd.
        """
        args = {"origin": origin, "destination": destination, "outbound_date": outbound_date, "return_date": return_date,
                "airline": airline, "seat_preference": seat_preference, "max_price_usd": max_price_usd}
        def read(state):
            departure, arrival = airport(origin), airport(destination)
            if departure == arrival:
                raise ValueError("Origin and destination must differ.")
            date_range(outbound_date, return_date, same_day=True)
            carrier = nonblank(airline, "airline").strip().casefold() if airline is not None else None
            if seat_preference is not None and seat_preference not in ("aisle", "window", "middle", "any"):
                raise ValueError("seat_preference must be aisle, window, middle, any, or null.")
            cap = amount_value(max_price_usd, allow_zero=True) if max_price_usd is not None else None
            matches = []
            for quote in state.get("flight_quotes", []):
                self._offer(quote, "flight")
                if (self._available(quote) and airport(quote["origin"]) == departure and airport(quote["destination"]) == arrival
                        and quote["outbound_date"] == outbound_date and quote["return_date"] == return_date
                        and (carrier is None or quote["airline"].strip().casefold() == carrier)
                        and (seat_preference in (None, "any") or quote["seat_preference"] == seat_preference)
                        and (cap is None or amount_value(quote["total_price_usd"], allow_zero=True) <= cap)):
                    matches.append(quote)
            return {"quotes": matches}
        return self.store.run("list_flight_quotes", args, read)

    def list_hotel_quotes(self, check_in: str, check_out: str, guests: int = 1, location: str | None = None,
                          hotel: str | None = None, max_price_usd: float | None = None) -> str:
        """List supplied hotel quotes for the requested dates and guest count.

        Dates are YYYY-MM-DD; check_out must follow check_in. guests is positive.
        Optional location and hotel match full names ignoring case and outer spaces.
        max_price_usd caps the total stay including fees, not the nightly price.
        Null means no cap. Returns available quotes and their ids and actual prices;
        no matching supplied offers returns an empty quotes list.
        """
        args = {"check_in": check_in, "check_out": check_out, "guests": guests,
                "location": location, "hotel": hotel, "max_price_usd": max_price_usd}
        def read(state):
            date_range(check_in, check_out)
            integer(guests, "guests")
            filters = {key: nonblank(value, key).strip().casefold() for key, value in (("location", location), ("hotel", hotel)) if value is not None}
            cap = amount_value(max_price_usd, allow_zero=True) if max_price_usd is not None else None
            matches = []
            for quote in state.get("hotel_quotes", []):
                self._offer(quote, "hotel")
                if (self._available(quote) and quote["check_in"] == check_in and quote["check_out"] == check_out
                        and quote["guests"] == guests and all(quote[key].strip().casefold() == value for key, value in filters.items())
                        and (cap is None or amount_value(quote["total_price_usd"], allow_zero=True) <= cap)):
                    matches.append(quote)
            return {"quotes": matches}
        return self.store.run("list_hotel_quotes", args, read)

    def _book(self, kind: str, args: dict, extra: dict) -> str:
        def book(state):
            quote = one_record(state.get(f"{kind}_quotes", []), args["quote_id"])
            self._offer(quote, kind)
            if not self._available(quote):
                raise ValueError("This quote is unavailable, expired, or already booked. Look up current quotes.")
            cap = args.get("max_price_usd")
            if cap is not None and amount_value(quote["total_price_usd"], allow_zero=True) > amount_value(cap, allow_zero=True):
                raise ValueError("The quoted total exceeds max_price_usd. Nothing was booked.")
            if kind == "flight":
                nonblank(extra["passenger_name"], "passenger_name")
                if not isinstance(extra["trip_purpose"], str):
                    raise ValueError("trip_purpose must be text.")
            row = self.store.new_record(f"{kind}_booking", {"quote_id": quote["id"], "quote": copy.deepcopy(quote),
                                                          "total_price_usd": quote["total_price_usd"], "currency": "USD",
                                                          "status": "booked", **extra})
            state.setdefault(f"{kind}_bookings", []).append(row)
            quote["available"] = False
            return {"ok": True, "booking": row}
        return self.store.run(f"book_{kind}", args, book, write=True)

    def book_flight(self, quote_id: str, passenger_name: str, max_price_usd: float | None = None, trip_purpose: str = "") -> str:
        """Book one available flight quote from list_flight_quotes for passenger_name.

        quote_id fixes the itinerary, airline, seat, and actual total price. The
        passenger's full name is required. max_price_usd is an optional total USD
        spending limit, never a quoted fare; null means no limit. trip_purpose is
        optional text. Returns the stored booking and a snapshot of its quote.
        Expired, unavailable, over-budget, or already booked offers fail without
        changing records. A successful booking consumes that offer.
        """
        args = {"quote_id": quote_id, "passenger_name": passenger_name, "max_price_usd": max_price_usd, "trip_purpose": trip_purpose}
        return self._book("flight", args, {"passenger_name": passenger_name, "trip_purpose": trip_purpose})

    def book_hotel(self, quote_id: str, max_price_usd: float | None = None) -> str:
        """Book an available hotel quote from list_hotel_quotes.

        quote_id fixes the hotel, location, check-in/out dates, guest count, and
        total stay price including fees. max_price_usd is an optional total USD
        limit, not a nightly limit; null means no limit. Returns the stored booking
        and quote snapshot. Invalid, expired, consumed, or over-budget offers fail
        without changing records. A successful booking consumes that offer.
        """
        return self._book("hotel", {"quote_id": quote_id, "max_price_usd": max_price_usd}, {})

    def search_venues(self, location_radius_miles: float = 100, capacity: int = 12,
                       max_price_per_night_usd: float | None = None) -> str:
        """Find supplied venues within a radius of the SF Bay Area reference point.

        location_radius_miles is a nonnegative distance; capacity is the positive
        number of people to accommodate. max_price_per_night_usd caps the whole
        venue's nightly USD price, not a per-person price; null means no cap.
        Returns venue records with distance_miles, capacity, and price_per_night.
        This search does not establish availability for particular dates.
        """
        args = {"location_radius_miles": location_radius_miles, "capacity": capacity,
                "max_price_per_night_usd": max_price_per_night_usd}
        def read(state):
            radius = distance(location_radius_miles)
            integer(capacity, "capacity")
            cap = amount_value(max_price_per_night_usd, allow_zero=True) if max_price_per_night_usd is not None else None
            matches = []
            for venue in state.get("venues", []):
                nonblank(venue.get("id"), "Venue id")
                nonblank(venue.get("name"), "Venue name")
                miles = distance(venue.get("distance_miles"))
                seats = integer(venue.get("capacity"), "Venue capacity")
                price = amount_value(venue.get("price_per_night"), allow_zero=True)
                if miles <= radius and seats >= capacity and (cap is None or price <= cap):
                    matches.append(venue)
            return matches
        return self.store.run("search_venues", args, read)
