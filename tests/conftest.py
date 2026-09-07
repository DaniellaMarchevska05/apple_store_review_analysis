from datetime import UTC, datetime
from typing import TypeVar
from uuid import uuid4

import pytest
from pydantic import BaseModel

from review_analysis.reviews.models import (
    AppSearchRequest,
    AppSearchResult,
    ClassificationBatch,
    Collection,
    CollectionMetadata,
    Review,
)
from review_analysis.reviews.text import clean_text

Output = TypeVar("Output", bound=BaseModel)


class FakeLLM:
    """Predictable provider replacement; no SDK or network in workflow tests."""

    model = "test-model"
    identity = "test-provider:test-model"
    calls = 0

    async def generate(
        self,
        prompt: str,
        data: list[dict[str, object]],
        schema: type[Output],
    ) -> tuple[Output, int, int]:
        self.calls += 1
        if schema is ClassificationBatch:
            result = {
                "reviews": [
                    {
                        "review_id": row["review_id"],
                        "language": "english",
                        "sentiment": "negative" if row["review_id"] in {"r1", "r2"} else "positive",
                        "complaint_phrases": ["invented phrase"]
                        if row["review_id"] == "r1"
                        else [],
                    }
                    for row in data
                ]
            }
        else:
            result = {
                "suggestions": [
                    {
                        "theme": "Crashes",
                        "problem_summary": "Reviewers report crashes, including after login.",
                        "recommendation": "Check crash logs.",
                        "validation": "Compare crash-free sessions after the fix.",
                        "evidence_ids": ["r0:text:0", "r1:text:0"],
                    }
                ],
                "strengths": [
                    {
                        "theme": "Enjoyable lessons",
                        "summary": "A reviewer enjoys the lessons.",
                        "evidence_ids": ["r2:text:0"],
                    }
                ],
            }
        return schema.model_validate(result), 10, 5


class FakeCollector:
    def __init__(self, collection: Collection) -> None:
        self.collection = collection

    async def collect(self, request: object) -> Collection:
        return self.collection

    async def search(self, request: AppSearchRequest) -> list[AppSearchResult]:
        return [
            AppSearchResult(
                app_id=self.collection.metadata.app_id,
                app_name=self.collection.metadata.app_name,
                developer="Test developer",
                app_url="https://apps.apple.com/us/app/id570060128",
            )
        ]


def make_review(
    review_id: str, text: str = "The app keeps crashing.", rating: int = 1, title: str = ""
) -> Review:
    return Review(
        id=review_id, title=title, text=text, rating=rating, cleaned_text=clean_text(title, text)
    )


def make_collection(reviews: list[Review]) -> Collection:
    return Collection(
        metadata=CollectionMetadata(
            id=str(uuid4()),
            app_id=570060128,
            app_name="Test app",
            country="us",
            collected_at=datetime.now(UTC),
            seed=42,
            sampled_count=len(reviews),
            pool_size=len(reviews),
            pages_fetched=1,
            discarded_entries=0,
            duplicate_entries=0,
            warnings=[],
        ),
        pool=reviews,
        sampled_ids=[r.id for r in reviews],
    )


@pytest.fixture
def collection() -> Collection:
    return make_collection(
        [
            make_review("r1", "The app keeps crashing. Please fix it.", 1),
            make_review("r2", "The app keeps crashing after login.", 2),
            make_review("r3", "I love the lessons!", 5),
            make_review("r4", "It has daily lessons.", 3),
        ]
    )
