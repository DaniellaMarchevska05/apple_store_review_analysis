from collections import Counter

from review_analysis.reviews.models import (
    Bucket,
    Classification,
    Phrase,
    RatingMetrics,
    Review,
    SentimentMetrics,
)
from review_analysis.reviews.text import clean_text

PHRASE_VERSION = "normalized-source-selected-multilingual-v3"
PHRASE_LIMIT = 15
PHRASE_MIN_REVIEWS = 2


def bucket(count: int, total: int) -> Bucket:
    return Bucket(count=count, percentage=round(100 * count / total, 2) if total else 0)


def rating_metrics(reviews: list[Review]) -> RatingMetrics:
    counts = Counter(review.rating for review in reviews)
    return RatingMetrics(
        review_count=len(reviews),
        average_rating=round(sum(r.rating for r in reviews) / len(reviews), 4) if reviews else None,
        distribution={str(star): bucket(counts[star], len(reviews)) for star in range(1, 6)},
    )


def sentiment_metrics(labels: list[Classification]) -> SentimentMetrics:
    counts: Counter[str] = Counter(
        label.sentiment for label in labels if label.sentiment is not None
    )
    analyzed = sum(counts.values())
    return SentimentMetrics(
        analyzed_count=analyzed,
        unsupported_count=len(labels) - analyzed,
        coverage_percentage=bucket(analyzed, len(labels)).percentage,
        distribution={
            name: bucket(counts[name], analyzed) for name in ("positive", "negative", "neutral")
        },
    )


def normalize_phrase(text: str) -> str:
    return clean_text("", text).casefold()


def negative_phrases(labels: list[Classification]) -> list[Phrase]:
    """Count verified source phrases per review; never combine translations into fake quotes."""
    matches: dict[str, set[str]] = {}
    originals: dict[str, str] = {}
    for label in labels:
        if label.sentiment != "negative":
            continue
        for phrase in label.complaint_phrases:
            key = normalize_phrase(phrase)
            originals.setdefault(key, phrase)
            matches.setdefault(key, set()).add(label.review_id)
    phrases = [
        Phrase(text=originals[key], review_count=len(ids), review_ids=sorted(ids))
        for key, ids in matches.items()
        if len(ids) >= PHRASE_MIN_REVIEWS
    ]
    return sorted(phrases, key=lambda p: (-p.review_count, p.text))[:PHRASE_LIMIT]
