import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from openai import AsyncOpenAI

from review_analysis.analysis.analyzer import ReviewAnalyzer
from review_analysis.analysis.llm import LLMClient
from review_analysis.app.config import Settings
from review_analysis.reviews.collector import AppleClient
from review_analysis.reviews.service import ReviewService
from review_analysis.reviews.storage import Store


@asynccontextmanager
async def open_service(settings: Settings) -> AsyncIterator[ReviewService]:
    """Create application services and close external clients on shutdown."""
    store = Store(settings.database_path)
    store.initialize()
    # Use the platform trust store, including organization-installed roots on Windows.
    # Verification stays enabled; SSL_CERT_FILE can configure roots on other platforms.
    context = ssl.create_default_context()
    async with httpx.AsyncClient(
        verify=context,
        headers={"User-Agent": "AppReviewAnalysis/0.1"},
    ) as http:
        key = settings.llm_api_key
        if key and key.get_secret_value():
            async with AsyncOpenAI(
                api_key=key.get_secret_value(),
                base_url=settings.llm_base_url,
                timeout=httpx.Timeout(settings.analysis_timeout_seconds, connect=10),
                max_retries=2,
                http_client=httpx.AsyncClient(verify=context),
            ) as client:
                yield ReviewService(
                    store,
                    AppleClient(http),
                    ReviewAnalyzer(LLMClient(client, settings.llm_model, settings.llm_api)),
                    collection_timeout_seconds=settings.collection_timeout_seconds,
                    analysis_timeout_seconds=settings.analysis_timeout_seconds,
                )
        else:
            yield ReviewService(
                store,
                AppleClient(http),
                None,
                collection_timeout_seconds=settings.collection_timeout_seconds,
                analysis_timeout_seconds=settings.analysis_timeout_seconds,
            )
