"""Synthetic expanded read models. Never captured account payloads."""

from __future__ import annotations

from typing import Any

from casahome.models.calendar import VisitsCalendarResponse
from casahome.models.home import HomeDetailsResponse
from casahome.models.mailbox import MailboxMailItemsResponse, PackagesResponse
from casahome.models.maintenance import AutopilotCategory, InventoryServiceHistoryResponse
from casahome.models.requests import RequestsResponse

HOME_ID = "00000000-0000-4000-8000-000000000002"


def inventory(*ids: str) -> HomeDetailsResponse:
    return HomeDetailsResponse.model_validate(
        {
            "home": {
                "id": HOME_ID,
                "display_address": "Synthetic address",
                "stories": [
                    {
                        "id": "story",
                        "floor_number": 1,
                        "name": "Floor",
                        "rooms": [{"id": "room", "name": "Test kitchen"}],
                    }
                ],
            },
            "entities": [
                {
                    "id": id,
                    "category_id": "appliances",
                    "category": "Appliances",
                    "status": "future_status",
                    "type": "Test oven",
                    "rooms": ["room"],
                    "make": {"id": "make", "name": "Test Make"},
                    "model": {
                        "id": "model",
                        "name": "Test Model",
                        "properties": [
                            {"id": "summary", "key": "summary", "value": "private-catalog-blob"}
                        ],
                    },
                    "entity_properties": [
                        {
                            "id": "serial",
                            "definition_id": "serial",
                            "title": "Serial",
                            "key": "serial_number",
                            "value": "SYNTHETIC-0001",
                        }
                    ],
                    "future_extra": "private-inventory-extra",
                }
                for id in ids
            ],
        }
    )


def request_card(id: str, **flags: Any) -> dict[str, Any]:
    return {
        "id": id,
        "type": "future_type",
        "title": "private-request-title",
        "emoji": "",
        "updated_at": 1,
        "latest_message_preview": "private-message",
        **flags,
    }


def requests(*cards: dict[str, Any]) -> RequestsResponse:
    return RequestsResponse.model_validate(
        {
            "requests": {
                "needs_review": list(cards),
                "sections": [
                    {
                        "name": "future",
                        "title": "private-section",
                        "items": list(cards),
                        "count": 999,
                    }
                ],
            },
            "tasks": {"sections": []},
        }
    )


def maintenance() -> list[AutopilotCategory]:
    return [
        AutopilotCategory.model_validate(
            {
                "category_id": "category",
                "category_title": "Test",
                "category_description": "private",
                "category_rendering_type": "future",
                "plans": [
                    {
                        "plan_id": "plan",
                        "plan_template_id": "template",
                        "title": "private-plan",
                        "subtitle": "private",
                        "description": "private",
                        "applies_to": "home",
                        "type": "future",
                        "enabled": True,
                        "illustration": {
                            "bg_color": "private",
                            "url": "https://example.invalid/signed",
                        },
                        "asap_schedule_description": "private",
                        "pricing_policies": [],
                        "fulfillment_policy": "future",
                        "savings_percentage": 0,
                        "cadence_options": [],
                        "default_cadence": "future",
                        "next_projected_occurrence_range_start": "opaque",
                        "next_projected_occurrence_range_end": "opaque",
                        "room_associations": [],
                        "entity_associations": [],
                        "price_extra": 999,
                    }
                ],
                "ineligible_plans": [],
            }
        )
    ]


def history() -> InventoryServiceHistoryResponse:
    return InventoryServiceHistoryResponse(autopilot_plans=[], history_items=[])


def mail() -> MailboxMailItemsResponse:
    return MailboxMailItemsResponse(mail_items=[])


def packages() -> PackagesResponse:
    return PackagesResponse(
        next_pickup_lockdown=0, next_pickup_start=0, next_pickup_end=0, packages=[]
    )


def calendar() -> VisitsCalendarResponse:
    return VisitsCalendarResponse(events=[])
