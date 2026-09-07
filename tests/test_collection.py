"""Sampling must be reproducible; an upstream failure must never look like an empty feed."""

import random

import httpx
import pytest

from review_analysis.reviews.collector import AppleClient
from review_analysis.reviews.models import AppError, CollectionRequest


def entry(index: int) -> dict:
    return {
        "id": {"label": str(index)},
        "title": {"label": "Title"},
        "content": {"label": "Text"},
        "im:rating": {"label": "4"},
    }


async def test_sampling_pagination_and_duplicates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/lookup":
            return httpx.Response(200, json={"results": [{"trackId": 1}]})
        page = int(str(request.url).split("page=")[1].split("/")[0])
        rows = [entry(i) for i in range((page - 1) * 50, page * 50)] + [entry(0), {"broken": True}]
        links = (
            [{"attributes": {"rel": "next", "href": "https://untrusted.invalid"}}]
            if page < 3
            else []
        )
        return httpx.Response(200, json={"feed": {"entry": rows, "link": links}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        snapshot = await AppleClient(http).collect(CollectionRequest(app_id=1, seed=42))
    assert snapshot.metadata.pool_size == 150
    assert snapshot.metadata.sampled_count == 100
    assert snapshot.metadata.duplicate_entries == snapshot.metadata.discarded_entries == 3
    assert snapshot.sampled_ids == [r.id for r in random.Random(42).sample(snapshot.pool, 100)]


@pytest.mark.parametrize("feed", [None, {"entry": "broken"}, {"entry": [{"broken": True}]}])
async def test_invalid_feed_is_not_an_empty_collection(feed: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/lookup":
            return httpx.Response(200, json={"results": [{"trackId": 1}]})
        return httpx.Response(200, json={"feed": feed})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(AppError) as error:
            await AppleClient(http).collect(CollectionRequest(app_id=1))
    assert error.value.code == "apple_response_error"
