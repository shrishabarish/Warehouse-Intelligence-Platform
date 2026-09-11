import io
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sqlalchemy.pool import StaticPool

from app.main import app
from app.db.database import get_db, Base
from app.db import models
from app.services.cloud_storage import cloud_storage

@pytest.fixture
def test_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()

@pytest.fixture
def client(test_db):
    def override_get_db():
        try:
            yield test_db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_cloud_storage_service_upload():
    """Verify CloudStorageService uploads and creates accessible paths/urls."""
    dummy_bytes = b"fake_mp4_video_content_for_testing"
    res = cloud_storage.upload_video(
        file_bytes=dummy_bytes,
        filename="test_dock_upload.mp4",
        content_type="video/mp4"
    )

    assert res["filename"] == "test_dock_upload.mp4"
    assert "cloud_url" in res
    assert "storage_key" in res
    assert res["size_bytes"] == len(dummy_bytes)
    assert Path(res["local_path"]).exists()


def test_video_upload_endpoint_e2e(client, test_db):
    """
    End-to-End Pipeline Test:
    Upload -> Save in DB -> Process -> Give Prediction -> Live Monitoring data ready
    """
    # Create or copy a small video for testing
    video_source_path = None
    for cand in [
        Path(__file__).resolve().parent.parent.parent / "frontend" / "public" / "videos" / "Rolling and dropping carton.mp4",
        Path(__file__).resolve().parent.parent.parent / "frontend" / "dist" / "videos" / "Rolling and dropping carton.mp4",
        Path(__file__).resolve().parent.parent / "storage" / "videos" / "Rolling and dropping carton.mp4"
    ]:
        if cand.exists():
            video_source_path = cand
            break

    if video_source_path:
        with open(video_source_path, "rb") as f:
            video_bytes = f.read()
    else:
        video_bytes = b"dummy_mp4_bytes_header"

    files = {
        "file": ("Rolling and dropping carton.mp4", io.BytesIO(video_bytes), "video/mp4")
    }
    data = {
        "bay_id": "Loading Bay 01",
        "camera_id": "CAM-01",
        "max_frames": "25"
    }

    response = client.post("/api/videos/upload", files=files, data=data)
    assert response.status_code == 200, f"Upload endpoint failed: {response.text}"

    body = response.json()

    # 1. Prediction Assertions
    assert body["status"] == "SUCCESS"
    assert body["video_id"] == "Rolling and dropping carton"
    assert "video_url" in body
    assert "risk_score" in body
    assert isinstance(body["risk_score"], (int, float))
    assert body["risk_level"] in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    assert "behaviors" in body
    assert isinstance(body["behaviors"], list)
    assert "timelineData" in body
    assert isinstance(body["timelineData"], list)
    assert "what_happened" in body
    assert "why_it_matters" in body
    assert "recommended_action" in body

    # 2. Database Persistence Assertions
    vid = test_db.query(models.Video).filter(models.Video.video_id == "Rolling and dropping carton").first()
    assert vid is not None, "Video record must be saved in database"
    assert vid.status == "COMPLETED", "Video status must be COMPLETED"
    assert vid.storage_key is not None, "Storage key must be set"
