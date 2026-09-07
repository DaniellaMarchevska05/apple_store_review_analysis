"""The user workflow: collect, analyze, reuse, download, and recover from failures."""

import csv
import io
from pathlib import Path

from conftest import FakeCollector, FakeLLM
from fastapi.testclient import TestClient

from review_analysis.analysis.analyzer import ReviewAnalyzer
from review_analysis.app.api import create_app
from review_analysis.app.config import Settings
from review_analysis.reviews.models import Collection
from review_analysis.reviews.service import ReviewService
from review_analysis.reviews.storage import Store


def test_complete_workflow_and_persistence(tmp_path: Path, collection: Collection) -> None:
    settings = Settings(_env_file=None, database_path=tmp_path / "reviews.sqlite")
    llm = FakeLLM()
    service = ReviewService(
        Store(settings.database_path),
        FakeCollector(collection),
        ReviewAnalyzer(llm),
        collection_timeout_seconds=settings.collection_timeout_seconds,
        analysis_timeout_seconds=settings.analysis_timeout_seconds,
    )
    with TestClient(create_app(settings, service)) as client:
        assert "Review Studio" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        storefronts = client.get("/api/v1/storefronts").json()
        assert len(storefronts) == 175 and storefronts["jp"] == "Japan"
        assert (
            client.get(
                "/api/v1/apps/search", params={"query": "app", "country": " JP "}
            ).status_code
            == 200
        )
        assert (
            client.get("/api/v1/apps/search", params={"query": "app", "country": "zz"}).status_code
            == 422
        )
        search = client.get("/api/v1/apps/search", params={"query": "Test app"})
        assert search.status_code == 200
        app_id = search.json()[0]["app_id"]
        assert client.get("/api/v1/apps/search", params={"query": "  "}).status_code == 422
        response = client.post("/api/v1/collections", json={"app_id": app_id})
        assert response.status_code == 201
        assert set(response.json()) == {
            "id",
            "app_id",
            "app_name",
            "country",
            "sampled_count",
            "warnings",
        }
        path = response.headers["Location"]
        assert client.get(path).json()["seed"] == collection.metadata.seed
        assert client.get(path + "/metrics").json()["average_rating"] == 2.75
        result = client.post(path + "/analysis")
        assert result.status_code == 200
        assert any("Discarded 1 unverified phrase" in w for w in result.json()["warnings"])
        assert result.json()["insights"][0]["supporting_review_count"] == 2
        assert "classifications" not in result.json()
        assert "validation" not in result.json()["insights"][0]
        assert result.json()["strengths"][0]["supporting_review_count"] == 1
        assert result.json()["strengths"][0]["evidence"][0]["review_id"] == "r3"
        assert len(result.json()["insights"][0]["examples"]) == 2
        details = client.get(path + "/analysis/details").json()
        assert len(details["classifications"]) == 4
        assert details["insights"][0]["validation"]
        assert details["sentiment"] == result.json()["sentiment"]
        assert client.post(path + "/analysis").json() == result.json()
        assert client.get(path + "/analysis").json() == result.json()
        assert llm.calls == 2
        assert len(client.get(path + "/reviews").json()) == 4
        assert (
            len(list(csv.DictReader(io.StringIO(client.get(path + "/reviews?format=csv").text))))
            == 4
        )
        assert client.get("/health").status_code == client.get("/openapi.json").status_code == 200
        for route in client.get("/openapi.json").json()["paths"].values():
            for operation in route.values():
                assert operation["description"]
                assert set(operation["responses"]) <= {"200", "201", "default"}
    assert Store(settings.database_path).get_collection(collection.metadata.id) == collection
    assert Store(settings.database_path).get_analysis(collection.metadata.id) is not None
