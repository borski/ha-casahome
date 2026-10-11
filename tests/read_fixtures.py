"""Entirely synthetic populated reads. No saved titles, identifiers, or timestamps."""

from casahome.models import OnboardingDetailsResponse, SuggestedTodosResponse

EVENT_START = 1_900_000_000


def suggestions(
    count: int = 10, *, status: str = "suggestion_concierge_approved"
) -> SuggestedTodosResponse:
    return SuggestedTodosResponse.model_validate(
        {
            "suggested_todos": [
                {
                    "id": f"synthetic-todo-{index}",
                    "title": f"Inspect sample item {index}",
                    "emoji": "",
                    "original_text": "synthetic-private-original",
                    "prepared_brief": "synthetic-private-brief https://example.invalid/brief",
                    "status": status,
                    "creator_role": "concierge",
                    "created_at": 1_800_000_000,
                    "updated_at": 1_800_000_001,
                    "is_suggested": True,
                    "can_delete": True,
                    "attachments": [
                        {
                            "id": "synthetic-attachment",
                            "name": "synthetic-private-photo",
                            "mimetype": "image/jpeg",
                            "size": 123,
                            "url": "https://example.invalid/signed-photo",
                            "cache_key": "synthetic-private-cache",
                            "thumbnail_url": "https://example.invalid/signed-thumbnail",
                        }
                    ],
                    "question_rounds": [{"text": "synthetic-private-question"}],
                    "destination": {"url": "https://example.invalid/private-destination"},
                    "unexpected_extra": "synthetic-private-extra",
                }
                for index in range(count)
            ],
            "unexpected_envelope": "synthetic-private-envelope",
        }
    )


def onboarding(*, start: int = EVENT_START) -> OnboardingDetailsResponse:
    return OnboardingDetailsResponse.model_validate(
        {
            "calendar_events": [
                {
                    "id": "synthetic-visit-later",
                    "start_at": start + 7200,
                    "end_at": start + 10800,
                    "title": "synthetic-private-title",
                    "location": "synthetic-private-location",
                    "url": "https://example.invalid/private-visit",
                },
                {"id": "synthetic-visit-first", "start_at": start, "end_at": start + 3600},
            ],
            "unexpected_envelope": "synthetic-private-calendar-envelope",
        }
    )
