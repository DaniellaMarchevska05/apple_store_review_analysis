from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from review_analysis.reviews.storefronts import normalize_country

Sentiment = Literal["positive", "negative", "neutral"]
Country = Annotated[str, BeforeValidator(normalize_country)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppSearchRequest(Model):
    query: str = Field(
        min_length=2,
        max_length=200,
        description="App name or search phrase.",
    )
    country: Country = Field(
        default="us",
        pattern=r"^[a-z]{2}$",
        description="Two-letter storefront code, such as us or ua.",
    )
    limit: int = Field(default=5, ge=1, le=20, description="Maximum number of matching apps.")

    @field_validator("query", mode="before")
    @classmethod
    def strip_input(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AppSearchResult(Model):
    app_id: int = Field(gt=0)
    app_name: str = Field(min_length=1)
    developer: str = Field(min_length=1)
    app_url: str = Field(min_length=1)


class CollectionSummary(Model):
    id: str
    app_id: int
    app_name: str
    country: str
    sampled_count: int
    warnings: list[str]


class CollectionRequest(Model):
    app_id: int = Field(
        gt=0,
        le=999999999999,
        strict=True,
        description="Apple app ID returned by app search, not a collection UUID.",
    )
    country: Country = Field(
        default="us",
        pattern=r"^[a-z]{2}$",
        description="Storefront to collect from; use the same country as your search.",
    )
    seed: int | None = Field(
        default=None,
        ge=0,
        le=2**63 - 1,
        strict=True,
        description=(
            "Optional seed: the same seed and saved pool reproduce the selection. "
            "Omit to generate one."
        ),
    )


class Review(Model):
    id: str = Field(min_length=1)
    title: str
    text: str
    rating: int = Field(ge=1, le=5)
    updated_at: datetime | None = None
    app_version: str | None = None
    cleaned_text: str


class CollectionMetadata(Model):
    id: str
    app_id: int
    app_name: str
    country: str
    collected_at: datetime
    seed: int
    requested_count: int = 100
    sampled_count: int
    pool_size: int
    pages_fetched: int
    discarded_entries: int
    duplicate_entries: int
    warnings: list[str]
    sampling_method: str = "uniform_without_replacement_from_accessible_recent_reviews"


class Collection(Model):
    metadata: CollectionMetadata
    pool: list[Review]
    sampled_ids: list[str]

    @model_validator(mode="after")
    def check_snapshot(self) -> "Collection":
        ids = {review.id for review in self.pool}
        if len(ids) != len(self.pool) or len(set(self.sampled_ids)) != len(self.sampled_ids):
            raise ValueError("Review IDs must be unique")
        if not set(self.sampled_ids) <= ids:
            raise ValueError("Sample must be contained in the saved pool")
        if self.metadata.pool_size != len(self.pool):
            raise ValueError("Pool size mismatch")
        if self.metadata.sampled_count != len(self.sampled_ids):
            raise ValueError("Sample size mismatch")
        if len(self.sampled_ids) != min(self.metadata.requested_count, len(self.pool)):
            raise ValueError("Sample must include the requested count or the entire smaller pool")
        return self

    def reviews(self) -> list[Review]:
        by_id = {review.id: review for review in self.pool}
        return [by_id[review_id] for review_id in self.sampled_ids]


class Bucket(Model):
    count: int
    percentage: float


class RatingMetrics(Model):
    review_count: int
    average_rating: float | None
    distribution: dict[str, Bucket]


class Classification(Model):
    review_id: str
    language: str = Field(min_length=2, max_length=80)
    sentiment: Sentiment | None
    has_complaint: bool = False
    complaint_phrases: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def check_language(self) -> "Classification":
        if (self.language == "unsupported") != (self.sentiment is None):
            raise ValueError("Only uninterpretable reviews must have a null sentiment")
        if self.sentiment != "negative" and self.complaint_phrases:
            raise ValueError("Recurring negative phrases must come from negative reviews")
        if self.sentiment is None and self.has_complaint:
            raise ValueError("Uninterpretable reviews cannot support a complaint")
        return self


class ClassificationBatch(Model):
    reviews: list[Classification]


class Evidence(Model):
    review_id: str
    excerpt: str = Field(min_length=1, max_length=500)


class Suggestion(Model):
    theme: str = Field(min_length=1, max_length=100)
    problem_summary: str = Field(default="", max_length=2000)
    recommendation: str = Field(min_length=1, max_length=2000)
    validation: str = Field(default="", max_length=1000)
    evidence: list[Evidence] = Field(min_length=1, max_length=100)


class Insight(Suggestion):
    supporting_review_count: int


class Strength(Model):
    theme: str
    summary: str
    supporting_review_count: int
    evidence: list[Evidence]


class Phrase(Model):
    text: str
    review_count: int
    review_ids: list[str]


class SentimentMetrics(Model):
    analyzed_count: int
    unsupported_count: int
    coverage_percentage: float
    distribution: dict[str, Bucket]


class Provenance(Model):
    model: str
    prompt_version: str
    preprocessing_version: str
    configuration_hash: str
    created_at: datetime
    input_tokens: int
    output_tokens: int
    duration_seconds: float


class Analysis(Model):
    collection_id: str
    provenance: Provenance
    classifications: list[Classification]
    sentiment: SentimentMetrics
    negative_phrases: list[Phrase]
    insights: list[Insight]
    strengths: list[Strength] | None = None
    warnings: list[str]


class PhraseSummary(Model):
    text: str
    review_count: int


class InsightSummary(Model):
    theme: str
    problem_summary: str
    recommendation: str
    supporting_review_count: int
    examples: list[Evidence] = Field(
        description="Up to two source excerpts. Full evidence is available from analysis/details."
    )


class AnalysisSummary(Model):
    collection_id: str
    model: str
    created_at: datetime
    language_scope: Literal["multilingual", "english"]
    sentiment: SentimentMetrics
    negative_phrases: list[PhraseSummary]
    insights: list[InsightSummary]
    strengths: list[Strength] | None
    warnings: list[str]

    @classmethod
    def from_analysis(cls, analysis: Analysis) -> "AnalysisSummary":
        return cls(
            collection_id=analysis.collection_id,
            model=analysis.provenance.model,
            created_at=analysis.provenance.created_at,
            language_scope=(
                "multilingual"
                if analysis.provenance.prompt_version.startswith("multilingual-")
                else "english"
            ),
            sentiment=analysis.sentiment,
            negative_phrases=[
                PhraseSummary(text=p.text, review_count=p.review_count)
                for p in analysis.negative_phrases
            ],
            insights=[
                InsightSummary(
                    theme=i.theme,
                    problem_summary=i.problem_summary,
                    recommendation=i.recommendation,
                    supporting_review_count=i.supporting_review_count,
                    examples=i.evidence[:2],
                )
                for i in analysis.insights
            ],
            strengths=(
                [s.model_copy(update={"evidence": s.evidence[:2]}) for s in analysis.strengths]
                if analysis.strengths is not None
                else None
            ),
            warnings=analysis.warnings,
        )


class ErrorDetail(Model):
    code: str
    message: str


class ErrorResponse(Model):
    error: ErrorDetail
    request_id: str


class AppError(Exception):
    """An expected failure safe to expose at the API boundary."""

    def __init__(self, code: str, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
