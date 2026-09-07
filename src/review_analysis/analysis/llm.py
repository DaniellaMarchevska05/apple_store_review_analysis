"""One boundary for structured generation; review logic never imports a provider SDK."""

import json
from typing import Literal, Protocol, TypeVar

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
)
from pydantic import BaseModel, ValidationError

from review_analysis.analysis.prompts import MAX_INPUT_CHARACTERS
from review_analysis.reviews.models import AppError

Output = TypeVar("Output", bound=BaseModel)


class StructuredLLM(Protocol):
    model: str
    identity: str

    async def generate(
        self,
        prompt: str,
        data: list[dict[str, object]],
        schema: type[Output],
    ) -> tuple[Output, int, int]:
        """Return a validated result, input tokens, and output tokens."""
        ...


class LLMClient:
    """Use Responses for OpenAI or structured Chat Completions for compatible endpoints."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        api: Literal["responses", "chat"] = "responses",
    ) -> None:
        self.client = client
        self.model = model
        self.api = api
        # Endpoint is part of cache identity; identical model names on different
        # providers must never reuse one another's results. This is hashed, not logged.
        self.identity = f"{api}:{client.base_url}:{model}:provider-defaults:v1"

    async def generate(
        self,
        prompt: str,
        data: list[dict[str, object]],
        schema: type[Output],
    ) -> tuple[Output, int, int]:
        content = json.dumps(data, ensure_ascii=False)
        # Include instructions, JSON overhead, schema, and any previous repair attempt.
        if (
            len(prompt) + len(content) + len(json.dumps(schema.model_json_schema()))
            > MAX_INPUT_CHARACTERS
        ):
            raise AppError("analysis_input_too_large", "AI request exceeds the text budget.", 422)
        try:
            if self.api == "responses":
                response = await self.client.responses.parse(
                    model=self.model,
                    input=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": content},
                    ],
                    text_format=schema,
                    max_output_tokens=12000,
                    store=False,
                )
                if response.status != "completed" or response.output_parsed is None:
                    raise AppError("invalid_ai_output", "AI response was incomplete or refused.")
                usage = response.usage
                return (
                    response.output_parsed,
                    usage.input_tokens if usage else 0,
                    usage.output_tokens if usage else 0,
                )
            completion = await self.client.chat.completions.parse(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": content},
                ],
                response_format=schema,
            )
            if not completion.choices:
                raise AppError("invalid_ai_output", "AI provider returned no choices.")
            choice = completion.choices[0]
            if choice.finish_reason != "stop" or choice.message.parsed is None:
                raise AppError("invalid_ai_output", "AI response was incomplete or refused.")
            chat_usage = completion.usage
            return (
                choice.message.parsed,
                chat_usage.prompt_tokens if chat_usage else 0,
                chat_usage.completion_tokens if chat_usage else 0,
            )
        except APITimeoutError as exc:
            raise AppError(
                "ai_timeout", "AI provider took too long to respond; reviews are saved.", 504
            ) from exc
        except APIConnectionError as exc:
            raise AppError("ai_unavailable", "AI provider could not be reached.", 503) from exc
        except APIStatusError as exc:
            raise AppError("ai_unavailable", "AI provider rejected the request.", 503) from exc
        except (
            ValidationError,
            ValueError,
            ContentFilterFinishReasonError,
            LengthFinishReasonError,
        ) as exc:
            raise AppError("invalid_ai_output", "AI response failed schema validation.") from exc
