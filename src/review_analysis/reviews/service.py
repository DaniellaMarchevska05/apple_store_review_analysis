import asyncio
import logging
from typing import Protocol

from review_analysis.reviews.models import (
    Analysis,
    AppError,
    AppSearchRequest,
    AppSearchResult,
    Collection,
    CollectionRequest,
)
from review_analysis.reviews.storage import Store

logger = logging.getLogger(__name__)


class Collector(Protocol):
    async def search(self, request: AppSearchRequest) -> list[AppSearchResult]: ...

    async def collect(self, request: CollectionRequest) -> Collection: ...


class Analyzer(Protocol):
    configuration_hash: str

    async def analyze(self, collection: Collection) -> Analysis: ...


class ReviewService:
    def __init__(
        self,
        store: Store,
        collector: Collector,
        analyzer: Analyzer | None,
        *,
        collection_timeout_seconds: float,
        analysis_timeout_seconds: float,
    ) -> None:
        self.store = store
        self.collector = collector
        self.analyzer = analyzer
        self.collection_timeout_seconds = collection_timeout_seconds
        self.analysis_timeout_seconds = analysis_timeout_seconds
        self._analysis_lock = asyncio.Lock()

    def get_collection(self, collection_id: str) -> Collection:
        return self.store.get_collection(collection_id)

    async def search_apps(self, request: AppSearchRequest) -> list[AppSearchResult]:
        try:
            async with asyncio.timeout(self.collection_timeout_seconds):
                return await self.collector.search(request)
        except TimeoutError as exc:
            raise AppError("search_timeout", "App search exceeded its deadline.", 504) from exc

    async def collect(self, request: CollectionRequest) -> Collection:
        try:
            async with asyncio.timeout(self.collection_timeout_seconds):
                collection = await self.collector.collect(request)
        except TimeoutError as exc:
            raise AppError("collection_timeout", "Collection exceeded its deadline.", 504) from exc
        await asyncio.to_thread(self.store.save_collection, collection)
        logger.info(
            "collection_saved id=%s count=%s", collection.metadata.id, len(collection.sampled_ids)
        )
        return collection

    async def analyze(self, collection_id: str) -> Analysis:
        collection = await asyncio.to_thread(self.get_collection, collection_id)
        if not collection.sampled_ids:
            raise AppError("empty_collection", "An empty collection cannot be analyzed.", 409)
        if self.analyzer is None:
            raise AppError("ai_not_configured", "Configure LLM_API_KEY to enable analysis.", 503)
        cached = await asyncio.to_thread(
            self.store.get_analysis,
            collection_id,
            self.analyzer.configuration_hash,
        )
        if cached is not None:
            return cached
        if self._analysis_lock.locked():
            raise AppError("analysis_busy", "An analysis is running; retry after it finishes.", 409)
        async with self._analysis_lock:
            try:
                async with asyncio.timeout(self.analysis_timeout_seconds):
                    analysis = await self.analyzer.analyze(collection)
            except TimeoutError as exc:
                raise AppError(
                    "analysis_timeout", "Analysis exceeded its deadline; reviews are saved.", 504
                ) from exc
            saved = await asyncio.to_thread(self.store.save_analysis, analysis)
        logger.info(
            "analysis_saved collection=%s duration=%s tokens=%s",
            collection_id,
            saved.provenance.duration_seconds,
            saved.provenance.input_tokens + saved.provenance.output_tokens,
        )
        return saved

    def get_analysis(self, collection_id: str) -> Analysis:
        self.get_collection(collection_id)
        result = self.store.get_analysis(collection_id)
        if result is None:
            raise AppError(
                "analysis_not_found", "No successful analysis exists for this collection.", 404
            )
        return result
