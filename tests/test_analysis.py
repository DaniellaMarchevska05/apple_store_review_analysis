"""Numerical invariants and the two supported LLM transport contracts."""

import json

import httpx
import pytest
from conftest import FakeLLM, make_collection, make_review
from openai import AsyncOpenAI
from pydantic import BaseModel

from review_analysis.analysis.analyzer import (
    ReviewAnalyzer,
    is_source_phrase,
    validate_classifications,
)
from review_analysis.analysis.evidence import (
    SelectedSuggestions,
    evidence_options,
    resolve_evidence,
    resolve_selections,
)
from review_analysis.analysis.llm import LLMClient, Output
from review_analysis.analysis.metrics import (
    negative_phrases,
    rating_metrics,
    sentiment_metrics,
)
from review_analysis.app.exports import reviews_json
from review_analysis.reviews.models import (
    AppError,
    Classification,
    ClassificationBatch,
    Collection,
    Evidence,
    Review,
)
from review_analysis.reviews.text import clean_text


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("not working", True),
        ("not\tworking", True),
        ("café", True),
        ("CAFÉ", False),  # Verification preserves case; only frequency grouping folds it.
        ("notworking", False),
        ("works well", False),
        ("Heading not", False),  # A phrase cannot cross the title/body boundary.
        (" \t ", False),
    ],
)
def test_phrase_verification_allows_only_source_normalization(phrase: str, expected: bool) -> None:
    review = make_review("r1", "not\n\tworking — cafe\u0301", title="Heading")
    assert is_source_phrase(phrase, review) is expected


async def test_normalized_sentiment_input_preserves_evidence_and_exports() -> None:
    collection = make_collection(
        [
            make_review("r1", "  Not\tworking!\n\nPlease fix. 😞  ", title=" Bad  app! "),
            make_review("r2", "Not working! Cafe\u0301 freezes. 😞"),
            make_review("r3", "I love the lessons!"),
        ]
    )
    original = collection.model_dump_json()
    exported = reviews_json(collection)

    class InspectingLLM(FakeLLM):
        async def generate(
            self, prompt: str, data: list[dict[str, object]], schema: type[Output]
        ) -> tuple[Output, int, int]:
            if schema is ClassificationBatch:
                assert data[0] == {
                    "review_id": "r1",
                    "title": "Bad app!",
                    "text": "Not working! Please fix. 😞",
                }
                assert data[1]["text"] == "Not working! Café freezes. 😞"
                return (
                    schema.model_validate(
                        {
                            "reviews": [
                                {
                                    "review_id": row["review_id"],
                                    "language": "english",
                                    "sentiment": "positive"
                                    if row["review_id"] == "r3"
                                    else "negative",
                                    "complaint_phrases": []
                                    if row["review_id"] == "r3"
                                    else ["Not working", "invented complaint"],
                                }
                                for row in data
                            ]
                        }
                    ),
                    10,
                    5,
                )
            return await super().generate(prompt, data, schema)

    result = await ReviewAnalyzer(InspectingLLM()).analyze(collection)
    assert collection.model_dump_json() == original
    assert reviews_json(collection) == exported
    assert result.insights[0].evidence[0].excerpt == collection.pool[0].text
    assert result.negative_phrases[0].text == "Not working"
    assert result.negative_phrases[0].review_count == 2
    assert result.classifications[0].complaint_phrases == ["Not working"]
    assert any("Discarded 2 unverified phrase" in warning for warning in result.warnings)


@pytest.mark.parametrize("text", ["Word. " * 300, "文" * 1200, "word " * 300, " " * 900 + "end"])
def test_evidence_chunks_preserve_source_text(text: str) -> None:
    review = make_review("long", text)
    payload, sources = evidence_options([review])
    assert len(payload) == 1
    assert sources
    for evidence_id, evidence in sources.items():
        start = int(evidence_id.rsplit(":", 1)[1])
        assert len(evidence.excerpt) <= 400
        assert evidence.excerpt == text[start : start + len(evidence.excerpt)]
    assert "".join(e.excerpt for e in sources.values()).strip() == text.strip()


def test_evidence_deduplicates_reviews_and_rejects_blank_excerpts() -> None:
    review = make_review("r1", "A crash happened. ", title="Crashes")
    _, sources = evidence_options([review])
    evidence = resolve_evidence([review], list(sources), sources)
    assert len(evidence) == 1
    assert evidence[0].excerpt == "Crashes"
    with pytest.raises(AppError):
        resolve_evidence([review], ["blank"], {"blank": Evidence(review_id="r1", excerpt=" ")})


def test_metrics_and_text_preservation(collection: Collection) -> None:
    ratings = rating_metrics(collection.reviews())
    assert ratings.average_rating == 2.75
    assert ratings.distribution["4"].count == 0
    assert rating_metrics([]).average_rating is None
    labels = [
        Classification(review_id="1", language="english", sentiment="negative"),
        Classification(review_id="2", language="unsupported", sentiment=None),
    ]
    sentiment = sentiment_metrics(labels)
    assert sentiment.coverage_percentage == 50
    assert sentiment.distribution["negative"].percentage == 100
    assert clean_text("Not good!", " I don't like it. 😞 ") == "Not good! I don't like it. 😞"
    phrase = negative_phrases(
        [
            Classification(
                review_id=str(i),
                language="japanese",
                sentiment="negative",
                complaint_phrases=["起動できない", "起動できない"],
            )
            for i in range(2)
        ]
    )[0]
    assert phrase.text == "起動できない"
    assert phrase.review_count == 2


def test_multilingual_source_validation_and_invalid_evidence() -> None:
    review = Review(
        id="r1",
        title="",
        text="Не працює вхід. Чудовий дизайн!",
        rating=2,
        cleaned_text="Не працює вхід. Чудовий дизайн!",
    )
    label = Classification(
        review_id="r1",
        language="ukrainian",
        sentiment="negative",
        has_complaint=True,
        complaint_phrases=["Не працює вхід"],
    )
    validate_classifications([review], [label])
    with pytest.raises(AppError):
        validate_classifications([review], [label, label])
    with pytest.raises(AppError):
        validate_classifications(
            [review], [label.model_copy(update={"complaint_phrases": ["crashes"]})]
        )
    _, sources = evidence_options([review])
    selected = SelectedSuggestions.model_validate(
        {
            "suggestions": [
                {
                    "theme": "Login",
                    "problem_summary": "Cannot log in.",
                    "recommendation": "Check login.",
                    "validation": "Monitor login failures.",
                    "evidence_ids": ["unknown"],
                }
            ]
        }
    )
    with pytest.raises(AppError):
        resolve_selections([review], selected, sources)


class Label(BaseModel):
    value: str


def provider_response(api: str, valid: bool = True) -> dict:
    if api == "chat":
        return {
            "id": "chat-1",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop" if valid else "length",
                    "message": {"role": "assistant", "content": '{"value":"ok"}'},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    return {
        "id": "resp-1",
        "object": "response",
        "created_at": 0,
        "model": "test",
        "status": "completed" if valid else "incomplete",
        "output": [
            {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": '{"value":"ok"}', "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("api", ["responses", "chat"])
async def test_provider_contracts(api: str, valid: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        assert data["model"] == "configurable-model"
        assert "temperature" not in data
        messages = data["input"] if api == "responses" else data["messages"]
        assert messages[0]["role"] == "system"
        assert "untrusted review" in messages[1]["content"]
        assert request.url.host == "provider.example"
        return httpx.Response(200, json=provider_response(api, valid))

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        AsyncOpenAI(
            api_key="test", base_url="https://provider.example/v1", http_client=http
        ) as sdk,
    ):
        llm = LLMClient(sdk, "configurable-model", api)
        if valid:
            result, inputs, outputs = await llm.generate(
                "System instructions", [{"text": "untrusted review"}], Label
            )
            assert result.value == "ok" and (inputs, outputs) == (10, 5)
        else:
            with pytest.raises(AppError) as error:
                await llm.generate("System instructions", [{"text": "untrusted review"}], Label)
            assert error.value.code == "invalid_ai_output"
