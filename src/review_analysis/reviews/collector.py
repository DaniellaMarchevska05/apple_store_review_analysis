import asyncio
import random
import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from pydantic import ValidationError

from review_analysis.reviews.models import (
    AppError,
    AppSearchRequest,
    AppSearchResult,
    Collection,
    CollectionMetadata,
    CollectionRequest,
    Review,
)
from review_analysis.reviews.text import clean_text

BASE_URL = "https://itunes.apple.com"


class AppleClient:
    """Adapter for Apple's public, undocumented customer-review feed."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def _get_json(self, url: str) -> dict[str, Any]:
        for attempt in range(3):
            try:
                response = await self.http.get(url, timeout=15)
                if (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
                    try:
                        delay = min(max(float(response.headers.get("retry-after", "0")), 0), 5)
                    except ValueError:
                        delay = 0
                    await asyncio.sleep(max(delay, 0.5 * 2**attempt))
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Expected a JSON object")
                return payload
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 2:
                    raise AppError("apple_unavailable", "Apple could not be reached.", 503) from exc
                await asyncio.sleep(0.5 * 2**attempt)
            except (httpx.HTTPStatusError, ValueError) as exc:
                raise AppError(
                    "apple_response_error", "Apple returned an unusable response."
                ) from exc
        raise AssertionError("Retry loop must return or raise")

    async def search(self, request: AppSearchRequest) -> list[AppSearchResult]:
        parameters = urlencode(
            {
                "term": request.query,
                "country": request.country,
                "entity": "software",
                "limit": request.limit,
            }
        )
        payload = await self._get_json(f"{BASE_URL}/search?{parameters}")
        entries = payload.get("results")
        if not isinstance(entries, list):
            raise AppError("apple_response_error", "Apple returned invalid search results.")
        try:
            return [
                AppSearchResult(
                    app_id=entry["trackId"],
                    app_name=entry["trackName"],
                    developer=entry["artistName"],
                    app_url=entry["trackViewUrl"],
                )
                for entry in entries
            ]
        except (KeyError, TypeError, ValidationError) as exc:
            raise AppError("apple_response_error", "Apple returned invalid app details.") from exc

    async def collect(self, request: CollectionRequest) -> Collection:
        lookup = await self._get_json(
            f"{BASE_URL}/lookup?id={request.app_id}&country={request.country}&entity=software"
        )
        if not isinstance(lookup.get("results"), list):
            raise AppError("apple_response_error", "Apple returned invalid app metadata.")
        if any(not isinstance(item, dict) for item in lookup["results"]):
            raise AppError("apple_response_error", "Apple returned invalid app metadata.")
        matches = [item for item in lookup["results"] if item.get("trackId") == request.app_id]
        if not matches:
            raise AppError("app_not_found", "App was not found in the selected storefront.", 404)
        app_name = str(matches[0].get("trackName", request.app_id))
        reviews: dict[str, Review] = {}
        discarded = duplicates = pages = 0
        warnings = [
            "Sample covers accessible recent reviews in one storefront, not all historical reviews."
        ]
        for page in range(1, 11):
            payload = await self._get_json(
                f"{BASE_URL}/{request.country}/rss/customerreviews/page={page}"
                f"/id={request.app_id}/sortby=mostrecent/json"
            )
            feed = payload.get("feed")
            if not isinstance(feed, dict):
                raise AppError("apple_response_error", "Apple returned an invalid review feed.")
            pages += 1
            entries = feed.get("entry", [])
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                raise AppError("apple_response_error", "Apple returned invalid review entries.")
            if not entries:
                break
            for entry in entries:
                try:
                    review = parse_review(entry)
                except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
                    discarded += 1
                    continue
                if review.id in reviews:
                    duplicates += 1
                    continue
                reviews[review.id] = review
            # Follow the pagination signal, but construct our own URLs: never fetch
            # arbitrary links returned by an upstream service.
            links = feed.get("link", [])
            if isinstance(links, dict):
                links = [links]
            if not isinstance(links, list):
                raise AppError("apple_response_error", "Apple returned invalid pagination.")
            if not any(
                isinstance(link, dict)
                and isinstance(link.get("attributes"), dict)
                and link["attributes"].get("rel") == "next"
                for link in links
            ):
                break
        if discarded and not reviews:
            raise AppError(
                "apple_response_error", "No usable reviews could be parsed from the feed."
            )
        pool = sorted(reviews.values(), key=lambda review: review.id)
        seed = request.seed if request.seed is not None else secrets.randbits(63)
        sampled = random.Random(seed).sample(pool, min(100, len(pool)))
        if len(pool) < 100:
            warnings.append(f"Only {len(pool)} valid reviews were available; requested 100.")
        if discarded:
            warnings.append(f"Skipped {discarded} malformed entries; see discarded_entries.")
        return Collection(
            metadata=CollectionMetadata(
                id=str(uuid4()),
                app_id=request.app_id,
                app_name=app_name,
                country=request.country,
                collected_at=datetime.now(UTC),
                seed=seed,
                sampled_count=len(sampled),
                pool_size=len(pool),
                pages_fetched=pages,
                discarded_entries=discarded,
                duplicate_entries=duplicates,
                warnings=warnings,
            ),
            pool=pool,
            sampled_ids=[review.id for review in sampled],
        )


def parse_review(entry: dict[str, Any]) -> Review:
    title = entry["title"]["label"]
    text = entry["content"]["label"]
    if not isinstance(title, str) or not isinstance(text, str) or not (title + text).strip():
        raise ValueError("Missing review text")
    updated = entry.get("updated", {}).get("label")
    return Review(
        id=entry["id"]["label"],
        title=title,
        text=text,
        rating=int(entry["im:rating"]["label"]),
        updated_at=updated,
        app_version=entry.get("im:version", {}).get("label"),
        cleaned_text=clean_text(title, text),
    )
