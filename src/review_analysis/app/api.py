import logging
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint

from review_analysis.analysis.metrics import rating_metrics
from review_analysis.app.config import Settings
from review_analysis.app.exports import reviews_csv, reviews_json
from review_analysis.app.runtime import open_service
from review_analysis.reviews.models import (
    Analysis,
    AnalysisSummary,
    AppError,
    AppSearchRequest,
    AppSearchResult,
    CollectionMetadata,
    CollectionRequest,
    CollectionSummary,
    ErrorResponse,
    RatingMetrics,
)
from review_analysis.reviews.service import ReviewService
from review_analysis.reviews.storefronts import STOREFRONTS

logger = logging.getLogger(__name__)


def get_service(request: Request) -> ReviewService:
    service: ReviewService = request.app.state.service
    return service


Service = Annotated[ReviewService, Depends(get_service)]
CollectionID = Annotated[
    UUID,
    Path(description="Collection UUID from POST /collections, not Apple's numeric app ID."),
]


def create_app(settings: Settings | None = None, service: ReviewService | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        if service is not None:
            service.store.initialize()
            app.state.service = service
            yield
            return
        async with open_service(settings) as initialized_service:
            app.state.service = initialized_service
            yield

    app = FastAPI(
        title="App Store Review Analysis",
        version="0.1.0",
        description=("Search for an app → collect reviews → view ratings and analysis."),
        lifespan=lifespan,
        responses={
            "default": {
                "model": ErrorResponse,
                "description": "Request could not be completed.",
            }
        },
        swagger_ui_parameters={
            "defaultModelsExpandDepth": -1,
            "defaultModelExpandDepth": 0,
            "docExpansion": "none",
        },
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = str(uuid4())
        start = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info(
            "request id=%s method=%s path=%s status=%s duration_ms=%.1f",
            request.state.request_id,
            request.method,
            request.url.path,
            response.status_code,
            (time.monotonic() - start) * 1000,
        )
        return response

    def error_response(request: Request, code: str, message: str, status: int) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            headers={"X-Request-ID": request.state.request_id},
            content={
                "error": {"code": code, "message": message},
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(AppError)
    async def expected_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "app_error request=%s code=%s status=%s cause=%s upstream_status=%s",
            request.state.request_id,
            exc.code,
            exc.status,
            type(exc.__cause__).__name__ if exc.__cause__ else None,
            getattr(exc.__cause__, "status_code", None),
        )
        return error_response(request, exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = "; ".join(
            f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors()
        )
        return error_response(request, "invalid_input", issues, 422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return error_response(request, "http_error", str(exc.detail), exc.status_code)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, exc: sqlite3.Error) -> JSONResponse:
        logger.error(
            "storage_error request=%s type=%s",
            request.state.request_id,
            type(exc).__name__,
            exc_info=exc,
        )
        return error_response(
            request, "storage_unavailable", "Storage is temporarily unavailable.", 503
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unexpected_error request=%s type=%s",
            request.state.request_id,
            type(exc).__name__,
            exc_info=exc,
        )
        return error_response(request, "internal_error", "An unexpected error occurred.", 500)

    @app.get("/health", tags=["system"], summary="Check API and database health")
    def health(service: Service) -> dict[str, str]:
        """Check that the service and database are available."""
        if not service.store.healthy():
            raise AppError("storage_unavailable", "Storage is temporarily unavailable.", 503)
        return {"status": "ok"}

    @app.get("/api/v1/storefronts", tags=["apps"], summary="List supported countries and regions")
    def storefronts() -> dict[str, str]:
        """Country codes and names available for app search and review collection."""
        return STOREFRONTS

    @app.get("/api/v1/apps/search", tags=["apps"], summary="Find an app ID by name")
    async def search_apps(
        parameters: Annotated[AppSearchRequest, Query()],
        service: Service,
    ) -> list[AppSearchResult]:
        """Find matching apps by name. Use a result's app_id to collect its reviews."""
        return await service.search_apps(parameters)

    @app.post(
        "/api/v1/collections",
        status_code=201,
        tags=["collections"],
        summary="Collect a random review sample",
    )
    async def collect(
        body: CollectionRequest, service: Service, response: Response
    ) -> CollectionSummary:
        """Save a random sample of up to 100 recent reviews and return its collection ID."""
        result = await service.collect(body)
        response.headers["Location"] = f"/api/v1/collections/{result.metadata.id}"
        return CollectionSummary.model_validate(result.metadata, from_attributes=True)

    @app.get(
        "/api/v1/collections/{collection_id}",
        tags=["collections"],
        summary="Inspect collection and sampling metadata",
    )
    def collection(collection_id: CollectionID, service: Service) -> CollectionMetadata:
        """View app details, collection date, sampling seed, and collection statistics."""
        return service.get_collection(str(collection_id)).metadata

    @app.get(
        "/api/v1/collections/{collection_id}/metrics",
        tags=["metrics"],
        summary="Get average rating and star distribution",
    )
    def metrics(collection_id: CollectionID, service: Service) -> RatingMetrics:
        """Get the average rating and count and percentage of each star rating."""
        return rating_metrics(service.get_collection(str(collection_id)).reviews())

    @app.post(
        "/api/v1/collections/{collection_id}/analysis",
        tags=["analysis"],
        summary="Analyze sentiment and improvement opportunities",
    )
    async def analyze(collection_id: CollectionID, service: Service) -> AnalysisSummary:
        """Analyze sentiment, common complaints, and improvement opportunities in the sample."""
        return AnalysisSummary.from_analysis(await service.analyze(str(collection_id)))

    @app.get(
        "/api/v1/collections/{collection_id}/analysis",
        tags=["analysis"],
        summary="Read the latest saved analysis",
    )
    def analysis(collection_id: CollectionID, service: Service) -> AnalysisSummary:
        """Get the latest saved findings and example excerpts."""
        return AnalysisSummary.from_analysis(service.get_analysis(str(collection_id)))

    @app.get(
        "/api/v1/collections/{collection_id}/analysis/details",
        tags=["analysis"],
        summary="Inspect full analysis evidence and diagnostics",
    )
    def analysis_details(collection_id: CollectionID, service: Service) -> Analysis:
        """Get individual review labels, all supporting excerpts, and analysis metadata."""
        return service.get_analysis(str(collection_id))

    @app.get(
        "/api/v1/collections/{collection_id}/reviews",
        tags=["exports"],
        summary="Download sampled reviews as JSON or CSV",
        response_class=Response,
        responses={200: {"content": {"application/json": {}, "text/csv": {}}}},
    )
    def download(
        collection_id: CollectionID,
        service: Service,
        format: Annotated[
            Literal["json", "csv"],
            Query(description="json for structured data; csv for spreadsheets."),
        ] = "json",
    ) -> Response:
        """Download the sampled reviews, including their original titles, text, and ratings."""
        saved = service.get_collection(str(collection_id))
        content = reviews_json(saved) if format == "json" else reviews_csv(saved)
        return Response(
            content,
            media_type="application/json" if format == "json" else "text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="reviews-{collection_id}.{format}"'
            },
        )

    static_directory = FilePath(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_directory), name="static")

    @app.get("/", include_in_schema=False)
    def interface() -> FileResponse:
        return FileResponse(static_directory / "index.html")

    return app
