import hashlib
import json
import time
from datetime import UTC, datetime

from review_analysis.analysis.evidence import (
    SelectedSuggestions,
    evidence_options,
    resolve_selections,
    resolve_strengths,
)
from review_analysis.analysis.llm import StructuredLLM
from review_analysis.analysis.metrics import (
    PHRASE_LIMIT,
    PHRASE_MIN_REVIEWS,
    PHRASE_VERSION,
    negative_phrases,
    sentiment_metrics,
)
from review_analysis.analysis.prompts import (
    BATCH_SIZE,
    CLASSIFICATION_PROMPT,
    EVIDENCE_REPAIR_PROMPT,
    INSIGHT_PROMPT,
    MAX_EXCERPT_CHARACTERS,
    MAX_INPUT_CHARACTERS,
    MAX_STRENGTHS,
    MAX_THEMES,
    PROMPT_VERSION,
)
from review_analysis.reviews.models import (
    Analysis,
    AppError,
    Classification,
    ClassificationBatch,
    Collection,
    Insight,
    Provenance,
    Review,
    Strength,
)
from review_analysis.reviews.text import PREPROCESSING_VERSION, normalize_text


def configuration_hash(model: str) -> str:
    settings = [
        model,
        PROMPT_VERSION,
        PREPROCESSING_VERSION,
        CLASSIFICATION_PROMPT,
        INSIGHT_PROMPT,
        EVIDENCE_REPAIR_PROMPT,
        {
            "batch_size": BATCH_SIZE,
            "max_input": MAX_INPUT_CHARACTERS,
            "excerpt_size": MAX_EXCERPT_CHARACTERS,
            "max_themes": MAX_THEMES,
            "max_strengths": MAX_STRENGTHS,
            "phrase_version": PHRASE_VERSION,
            "phrase_limit": PHRASE_LIMIT,
            "phrase_min_reviews": PHRASE_MIN_REVIEWS,
        },
    ]
    return hashlib.sha256(json.dumps(settings).encode()).hexdigest()


def is_source_phrase(phrase: str, review: Review) -> bool:
    """Apply the same source and length rules during filtering and final validation."""
    normalized = normalize_text(phrase)
    return 2 <= len(normalized) <= 100 and (
        normalized in normalize_text(review.title) or normalized in normalize_text(review.text)
    )


def validate_classifications(reviews: list[Review], labels: list[Classification]) -> None:
    expected = {review.id for review in reviews}
    actual = [label.review_id for label in labels]
    if len(actual) != len(expected) or set(actual) != expected:
        raise AppError(
            "invalid_ai_output", "AI response did not classify each submitted review once."
        )
    by_id = {review.id: review for review in reviews}
    for label in labels:
        review = by_id[label.review_id]
        for phrase in label.complaint_phrases:
            if not is_source_phrase(phrase, review):
                raise AppError("invalid_ai_output", "AI returned a phrase absent from its review.")


class ReviewAnalyzer:
    def __init__(self, llm: StructuredLLM) -> None:
        self.llm = llm
        self.model = llm.model
        self.configuration_hash = configuration_hash(llm.identity)

    async def analyze(self, collection: Collection) -> Analysis:
        start = time.monotonic()
        reviews = collection.reviews()
        # Bound total prompt size without silently truncating evidence or dropping reviews.
        if (
            sum(
                max(len(review.cleaned_text), len(review.title) + len(review.text))
                for review in reviews
            )
            > MAX_INPUT_CHARACTERS
        ):
            raise AppError(
                "analysis_input_too_large", "Sample exceeds the analysis text budget.", 422
            )
        labels: list[Classification] = []
        input_tokens = output_tokens = 0
        discarded_phrases = 0
        for offset in range(0, len(reviews), BATCH_SIZE):
            batch = reviews[offset : offset + BATCH_SIZE]
            result, used_input, used_output, discarded = await self.classify(batch)
            labels.extend(result.reviews)
            input_tokens += used_input
            output_tokens += used_output
            discarded_phrases += discarded
        validate_classifications(reviews, labels)
        interpretable_ids = {label.review_id for label in labels if label.sentiment is not None}
        interpretable = [review for review in reviews if review.id in interpretable_ids]
        strengths: list[Strength] = []
        insights: list[Insight] = []
        if interpretable:
            payload, sources = evidence_options(interpretable)
            suggestions, used_input, used_output = await self.llm.generate(
                INSIGHT_PROMPT,
                payload,
                SelectedSuggestions,
            )
            input_tokens += used_input
            output_tokens += used_output
            try:
                insights = resolve_selections(interpretable, suggestions, sources)
                strengths = resolve_strengths(interpretable, suggestions, sources)
            except AppError:
                # One repair inside the existing operation deadline. Never weaken
                # evidence checks or rerun already completed sentiment batches.
                suggestions, used_input, used_output = await self.llm.generate(
                    INSIGHT_PROMPT + EVIDENCE_REPAIR_PROMPT,
                    payload + [{"previous_attempt": suggestions.model_dump_json()}],
                    SelectedSuggestions,
                )
                input_tokens += used_input
                output_tokens += used_output
                insights = resolve_selections(interpretable, suggestions, sources)
                strengths = resolve_strengths(interpretable, suggestions, sources)
        sentiment = sentiment_metrics(labels)
        warnings = [
            "AI sentiment and theme membership can be wrong; evidence matching verifies excerpts, "
            "not their interpretation. Recommendations are hypotheses to investigate."
        ]
        warnings.append(
            "Language accuracy varies. Themes and source phrases are selected findings, "
            "not an exhaustive inventory; frequency does not measure severity."
        )
        if sentiment.unsupported_count:
            warnings.append("Uninterpretable reviews are excluded from sentiment percentages.")
        if discarded_phrases:
            warnings.append(
                f"Discarded {discarded_phrases} unverified phrase selections; "
                "recurring phrase coverage may be incomplete."
            )
        return Analysis(
            collection_id=collection.metadata.id,
            provenance=Provenance(
                model=self.model,
                prompt_version=PROMPT_VERSION,
                preprocessing_version=PREPROCESSING_VERSION,
                configuration_hash=self.configuration_hash,
                created_at=datetime.now(UTC),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_seconds=round(time.monotonic() - start, 3),
            ),
            classifications=labels,
            sentiment=sentiment,
            negative_phrases=negative_phrases(labels),
            insights=insights,
            strengths=strengths,
            warnings=warnings,
        )

    async def classify(self, reviews: list[Review]) -> tuple[ClassificationBatch, int, int, int]:
        """Classify one batch and verify that every review received exactly one label."""
        result, input_tokens, output_tokens = await self.llm.generate(
            CLASSIFICATION_PROMPT,
            [
                {
                    "review_id": review.id,
                    "title": normalize_text(review.title),
                    "text": normalize_text(review.text),
                }
                for review in reviews
            ],
            ClassificationBatch,
        )
        by_id = {review.id: review for review in reviews}
        discarded = 0
        for label in result.reviews:
            review = by_id.get(label.review_id)
            if review is None:
                continue  # The completeness check below rejects unknown review IDs.
            verified = [
                normalize_text(phrase)
                for phrase in label.complaint_phrases
                if is_source_phrase(phrase, review)
            ]
            discarded += len(label.complaint_phrases) - len(verified)
            label.complaint_phrases = verified
        validate_classifications(reviews, result.reviews)
        return result, input_tokens, output_tokens, discarded
