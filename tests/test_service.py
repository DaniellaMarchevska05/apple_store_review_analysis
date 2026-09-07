"""An interrupted analysis must release its lock and leave the saved sample usable."""

import asyncio
from pathlib import Path

import pytest
from conftest import FakeCollector, FakeLLM

from review_analysis.analysis.analyzer import ReviewAnalyzer
from review_analysis.app.config import Settings
from review_analysis.reviews.models import Analysis, AppError, Collection
from review_analysis.reviews.service import ReviewService
from review_analysis.reviews.storage import Store


class BlockingAnalyzer(ReviewAnalyzer):
    def __init__(self) -> None:
        super().__init__(FakeLLM())
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def analyze(self, collection: Collection) -> Analysis:
        self.started.set()
        await self.release.wait()
        return await super().analyze(collection)


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_analysis_failure_allows_retry(
    tmp_path: Path, collection: Collection, failure: str
) -> None:
    settings = Settings(
        _env_file=None,
        analysis_timeout_seconds=0.5 if failure == "timeout" else 10,
    )
    store = Store(tmp_path / "reviews.sqlite3")
    store.initialize()
    store.save_collection(collection)
    analyzer = BlockingAnalyzer()
    service = ReviewService(
        store,
        FakeCollector(collection),
        analyzer,
        collection_timeout_seconds=settings.collection_timeout_seconds,
        analysis_timeout_seconds=settings.analysis_timeout_seconds,
    )
    task = asyncio.create_task(service.analyze(collection.metadata.id))
    try:
        await asyncio.wait_for(analyzer.started.wait(), timeout=5)
        with pytest.raises(AppError) as busy:
            await service.analyze(collection.metadata.id)
        assert busy.value.code == "analysis_busy"
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(AppError) as timeout:
                await task
            assert timeout.value.code == "analysis_timeout"
        assert store.get_analysis(collection.metadata.id) is None
        assert store.get_collection(collection.metadata.id) == collection
        analyzer.release.set()
        result = await service.analyze(collection.metadata.id)
        assert result.collection_id == collection.metadata.id
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
