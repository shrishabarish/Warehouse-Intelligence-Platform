import os
import io
import time
import logging
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from typing import Dict, Any, Optional
from app.config import settings

logger = logging.getLogger(__name__)

class CloudStorageService:
    """
    Cloud Storage Integration Service.
    Uploads warehouse CCTV and video clips directly to Cloud Storage (Supabase Storage bucket),
    and retrieves permanent public URLs for live streaming and browser replay.
    """

    def __init__(self):
        self.supabase_url = settings.SUPABASE_URL.rstrip("/") if settings.SUPABASE_URL else ""
        self.supabase_key = settings.SUPABASE_KEY
        self.bucket = settings.SUPABASE_STORAGE_BUCKET or "videos"
        self.local_cache_dir = Path(__file__).resolve().parent.parent.parent / "storage" / "videos"
        self.local_cache_dir.mkdir(parents=True, exist_ok=True)

    def is_cloud_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_key and not self.supabase_key.startswith("mock_"))

    def upload_video(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str = "video/mp4"
    ) -> Dict[str, Any]:
        """
        Uploads a video to Cloud Storage (Supabase Storage).
        Also caches a copy in storage/videos/ to allow immediate zero-latency CV frame reading.
        """
        clean_filename = Path(filename).name
        timestamp_prefix = int(time.time())
        unique_cloud_name = f"{timestamp_prefix}_{clean_filename}"

        # 1. Always cache locally so OpenCV / YOLO can process frames directly
        cached_path = self.local_cache_dir / clean_filename
        with open(cached_path, "wb") as f:
            f.write(file_bytes)

        # 2. Attempt Supabase Cloud Storage Upload if configured
        cloud_url = None
        storage_key = unique_cloud_name
        cloud_status = "LOCAL_STAGED"

        if self.is_cloud_configured():
            try:
                # Supabase Storage REST API: POST /storage/v1/object/{bucket}/{path}
                upload_endpoint = f"{self.supabase_url}/storage/v1/object/{self.bucket}/{urllib.parse.quote(unique_cloud_name)}"
                req = urllib.request.Request(
                    upload_endpoint,
                    data=file_bytes,
                    headers={
                        "Authorization": f"Bearer {self.supabase_key}",
                        "apikey": self.supabase_key,
                        "Content-Type": content_type,
                        "x-upsert": "true"
                    },
                    method="POST"
                )

                with urllib.request.urlopen(req, timeout=15.0) as resp:
                    if resp.status in [200, 201]:
                        # Public URL format for Supabase Storage
                        cloud_url = f"{self.supabase_url}/storage/v1/object/public/{self.bucket}/{urllib.parse.quote(unique_cloud_name)}"
                        cloud_status = "UPLOADED_TO_CLOUD"
                        storage_key = cloud_url
                        logger.info(f"[CloudStorage] Successfully uploaded '{clean_filename}' to Supabase Storage: {cloud_url}")
            except Exception as e:
                logger.warning(f"[CloudStorage] Cloud upload warning ({e}). Video staged locally at {cached_path}")
                cloud_status = "CLOUD_PENDING"

        # If not cloud configured or fallback, use streaming endpoint
        if not cloud_url:
            cloud_url = f"/api/videos/stream/{urllib.parse.quote(clean_filename)}"

        return {
            "filename": clean_filename,
            "cloud_filename": unique_cloud_name,
            "cloud_url": cloud_url,
            "storage_key": storage_key,
            "status": cloud_status,
            "local_path": str(cached_path),
            "size_bytes": len(file_bytes)
        }

    def get_public_url(self, storage_key: str, filename: str) -> str:
        if storage_key and storage_key.startswith("http"):
            return storage_key
        return f"/api/videos/stream/{urllib.parse.quote(filename)}"


# Singleton instance
cloud_storage = CloudStorageService()
