"""Synthetic, non-production data used by the offline evaluation scenarios."""

from __future__ import annotations


SYNTHETIC_LOAD = {
    "id": "EVAL-1042",
    "load_id": "EVAL-1042",
    "origin": {"city": "Columbus", "state": "OH"},
    "destination": {"city": "Nashville", "state": "TN"},
    "pickupTime": "October 14th at 8 AM",
    "dropoffTime": "October 15th by noon",
    "equipment": "Dry van",
    "commodity": "Paper goods",
    "specialInstructions": "Driver must call before pickup",
    "trackerRequired": True,
    "startRate": 1800.0,
    "bookNowRate": 1950.0,
    "maxRate": 2200.0,
    "transfer_call_to": "+15550101042",
    "transfer_country_code": "+1",
}

SYNTHETIC_ORG_NAME = "Evaluation Freight"
SYNTHETIC_CARRIER_NAME = "Example Carrier LLC"
