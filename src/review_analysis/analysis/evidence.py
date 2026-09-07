"""Build source excerpts and resolve model selections into verified findings."""

import re

from pydantic import Field

from review_analysis.analysis.prompts import MAX_EXCERPT_CHARACTERS, MAX_STRENGTHS, MAX_THEMES
from review_analysis.reviews.models import (
    AppError,
    Evidence,
    Insight,
    Model,
    Review,
    Strength,
)


class SelectedSuggestion(Model):
    theme: str = Field(min_length=1, max_length=100)
    problem_summary: str = Field(min_length=1, max_length=2000)
    recommendation: str = Field(min_length=1, max_length=2000)
    validation: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)


class SelectedStrength(Model):
    theme: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)


class SelectedSuggestions(Model):
    suggestions: list[SelectedSuggestion] = Field(max_length=MAX_THEMES)
    strengths: list[SelectedStrength] = Field(default_factory=list, max_length=MAX_STRENGTHS)


def evidence_options(reviews: list[Review]) -> tuple[list[dict[str, object]], dict[str, Evidence]]:
    """Give the model stable source choices instead of asking it to reproduce quotations."""
    payload: list[dict[str, object]] = []
    sources: dict[str, Evidence] = {}
    for index, review in enumerate(reviews):
        options = []
        for field, content in (("title", review.title), ("text", review.text)):
            start = 0
            while start < len(content):
                end = min(start + MAX_EXCERPT_CHARACTERS, len(content))
                if end < len(content):
                    sentences = list(re.finditer(r"[。！？]|[.!?](?=\s|$)", content[start:end]))
                    if sentences:
                        end = start + sentences[-1].start() + 1
                    else:
                        boundary = content.rfind(" ", start, end)
                        if boundary > start:
                            end = boundary
                excerpt = content[start:end]
                if excerpt.strip():
                    evidence_id = f"r{index}:{field}:{start}"
                    sources[evidence_id] = Evidence(review_id=review.id, excerpt=excerpt)
                    options.append({"evidence_id": evidence_id, "excerpt": excerpt})
                start = end
        payload.append({"review_id": review.id, "evidence_options": options})
    return payload, sources


def resolve_selections(
    reviews: list[Review],
    selections: SelectedSuggestions,
    sources: dict[str, Evidence],
) -> list[Insight]:
    suggestions = []
    for selected in selections.suggestions:
        evidence = resolve_evidence(reviews, selected.evidence_ids, sources)
        suggestions.append(
            Insight(
                theme=selected.theme,
                problem_summary=selected.problem_summary,
                recommendation=selected.recommendation,
                validation=selected.validation,
                evidence=evidence,
                supporting_review_count=len(evidence),
            )
        )
    return sorted(suggestions, key=lambda item: (-item.supporting_review_count, item.theme))


def resolve_evidence(
    reviews: list[Review],
    ids: list[str],
    sources: dict[str, Evidence],
) -> list[Evidence]:
    by_id = {review.id: review for review in reviews}
    selected: dict[str, Evidence] = {}
    for source_id in ids:
        evidence = sources.get(source_id)
        review = by_id.get(evidence.review_id) if evidence else None
        if (
            evidence is None
            or review is None
            or not evidence.excerpt.strip()
            or not (evidence.excerpt in review.title or evidence.excerpt in review.text)
        ):
            raise AppError("invalid_ai_output", "AI selected unverifiable evidence.")
        selected.setdefault(evidence.review_id, evidence)
    return list(selected.values())


def resolve_strengths(
    reviews: list[Review],
    selections: SelectedSuggestions,
    sources: dict[str, Evidence],
) -> list[Strength]:
    strengths = []
    for selected in selections.strengths:
        evidence = resolve_evidence(reviews, selected.evidence_ids, sources)
        strengths.append(
            Strength(
                theme=selected.theme,
                summary=selected.summary,
                evidence=evidence,
                supporting_review_count=len(evidence),
            )
        )
    return sorted(strengths, key=lambda s: (-s.supporting_review_count, s.theme))
