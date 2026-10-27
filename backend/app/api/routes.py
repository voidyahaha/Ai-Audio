"""
FastAPI route definitions.
All endpoints require Firebase ID token authentication.
"""

import os
import re
import subprocess
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Header
from fastapi.responses import JSONResponse
from loguru import logger
from typing import Optional

from app.models import (
    SeparationRequest, MixRequest, AnalyzeRequest,
    HealthResponse, Job, JobStatus,
)
from app.config import settings
from app.services.firebase_service import (
    update_job_status, get_job, get_user_jobs, upload_to_storage, firebase_ready,
)
from app.services.job_manager import (
    dispatch_separation, dispatch_vocal_mix,
    get_active_job_count, cancel_job,
)
from app.services.audio_analysis import analyze_audio
from app.services.demucs_service import GPU_AVAILABLE, DEVICE, CPU_THREADS

router = APIRouter(prefix="/api")

# ── Auth helper ──────────────────────────────────────────────────────────────

async def verify_firebase_token(authorization: Optional[str] = Header(None)) -> dict:
    """Verify a Firebase ID token, including Firebase Anonymous Auth tokens."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.replace("Bearer ", "").strip()
    if not firebase_ready():
        raise HTTPException(
            status_code=503,
            detail="Firebase Admin is not configured. Add backend credentials to verify signed-in users.",
        )

    try:
        from firebase_admin import auth as fb_auth
        decoded = fb_auth.verify_id_token(token, clock_skew_seconds=60)
        return decoded
    except Exception:
        logger.warning("Firebase token verification failed")
        raise HTTPException(status_code=401, detail="Invalid or expired authentication token")


def _validate_job_id(job_id: str) -> None:
    """Restrict Firestore IDs and temp-directory names to safe characters."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id or ""):
        raise HTTPException(status_code=400, detail="Invalid job ID")


def _is_safe_storage_path(storage_path: str) -> bool:
    """Reject absolute paths, traversal segments, and platform path separators."""
    if not storage_path or "\\" in storage_path or storage_path.startswith("/"):
        return False
    parts = storage_path.split("/")
    return (
        len(parts) >= 4
        and parts[0] == "users"
        and all(part not in {"", ".", ".."} for part in parts)
    )


def _assert_owned_storage_path(storage_path: str, user_id: str) -> None:
    """Ensure an Admin-SDK operation cannot be pointed at another user's file."""
    prefix = f"users/{user_id}/"
    if not _is_safe_storage_path(storage_path) or not storage_path.startswith(prefix):
        raise HTTPException(status_code=403, detail="Storage path does not belong to this user")


def _validate_playable_audio(path: str) -> None:
    """Reject a corrupt payload before it is persisted in Cloud Storage."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if probe.returncode != 0 or float(probe.stdout.strip()) <= 0:
            raise ValueError("unplayable audio")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise HTTPException(status_code=400, detail="The uploaded file is not valid playable audio")


# ── Health ───────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint — no auth required."""
    return HealthResponse(
        status="ok",
        gpu_available=GPU_AVAILABLE,
        demucs_model=settings.DEMUCS_MODEL,
        active_jobs=get_active_job_count(),
        device=DEVICE,
        cpu_threads=CPU_THREADS,
    )


@router.get("/files/{file_path:path}")
async def serve_file(
    file_path: str,
    token_data: dict = Depends(verify_firebase_token),
):
    """Serve a user's audio file from local storage after verifying ownership."""
    from fastapi.responses import FileResponse
    from app.services.firebase_service import LOCAL_STORAGE_DIR

    if not _is_safe_storage_path(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    _assert_owned_storage_path(file_path, token_data.get("uid", ""))

    base_dir = os.path.abspath(LOCAL_STORAGE_DIR)
    target_path = os.path.abspath(os.path.join(base_dir, file_path))

    # Path traversal protection
    try:
        inside_storage = os.path.commonpath([base_dir, target_path]) == base_dir
    except ValueError:
        inside_storage = False
    if not inside_storage or not os.path.isfile(target_path):
        raise HTTPException(status_code=404, detail="File not found")

    ext = os.path.splitext(target_path)[1].lower()
    media_type = "audio/mpeg" if ext == ".mp3" else "audio/wav" if ext == ".wav" else "audio/webm" if ext == ".webm" else "application/octet-stream"
    return FileResponse(target_path, media_type=media_type, filename=os.path.basename(target_path))



# ── Upload ───────────────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_audio(
    file: UploadFile = File(...),
    user_id: str = Form(...),
    folder: str = Form(default="originals"),
    token_data: dict = Depends(verify_firebase_token),
):
    """
    Upload audio file to Firebase Storage via backend.
    Validates file type and size before uploading.
    """
    # Validate user matches token
    if token_data.get("uid") != user_id:
        raise HTTPException(status_code=403, detail="User ID mismatch")

    # Validate content type
    allowed_types = {
        "audio/mpeg", "audio/mp3", "audio/wav", "audio/wave",
        "audio/x-wav", "audio/flac", "audio/x-flac", "audio/mp4",
        "audio/m4a", "audio/aac", "audio/ogg", "audio/webm",
        "video/mp4",
    }
    content_type = file.content_type or ""
    filename = Path((file.filename or "audio").replace("\\", "/")).name
    ext = os.path.splitext(filename)[1].lower()
    allowed_exts = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".webm"}

    if content_type not in allowed_types and ext not in allowed_exts:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {content_type or ext}"
        )

    # Stream to disk so a large upload does not consume the API process's RAM.
    MAX_SIZE = settings.MAX_UPLOAD_MB * 1024 * 1024

    if folder not in {"originals", "vocals", "instrumentals"}:
        raise HTTPException(status_code=400, detail="Unsupported upload folder")

    # Save to temp
    temp_path = os.path.join(settings.PROCESSING_TEMP_DIR, f"upload_{uuid.uuid4()}{ext}")
    try:
        size = 0
        with open(temp_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_SIZE:
                    raise HTTPException(status_code=413, detail=f"File too large (max {settings.MAX_UPLOAD_MB}MB)")
                f.write(chunk)
        if size < 1000:
            raise HTTPException(status_code=400, detail="File too small to be valid audio")
        _validate_playable_audio(temp_path)

        # Upload to Firebase Storage
        timestamp = int(__import__("time").time())
        storage_path = (
            f"users/{user_id}/{folder}/"
            f"{timestamp}_{uuid.uuid4().hex[:8]}_{filename}"
        )
        download_url = await upload_to_storage(
            temp_path, storage_path, content_type or "audio/mpeg"
        )

        return {
            "success": True,
            "storage_path": storage_path,
            "download_url": download_url,
            "file_name": filename,
            "size": size,
        }
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


# ── Separation ───────────────────────────────────────────────────────────────

@router.post("/separate")
async def start_separation(
    request: SeparationRequest,
    token_data: dict = Depends(verify_firebase_token),
):
    """Enqueue a Demucs stem separation job."""
    _validate_job_id(request.job_id)
    if token_data.get("uid") != request.user_id:
        raise HTTPException(status_code=403, detail="User ID mismatch")

    if not request.storage_path:
        raise HTTPException(status_code=400, detail="storage_path is required")
    _assert_owned_storage_path(request.storage_path, request.user_id)

    if request.model not in {"htdemucs_ft", "htdemucs", "htdemucs_6s", "mdx_extra"}:
        raise HTTPException(status_code=400, detail="Unsupported Demucs model")

    existing_job = await get_job(request.job_id)
    if existing_job and existing_job.get("userId") != token_data.get("uid"):
        raise HTTPException(status_code=403, detail="Job does not belong to this user")
    if existing_job and existing_job.get("status") in {"processing", "separating", "mixing", "completed"}:
        raise HTTPException(status_code=409, detail="This job has already been submitted")
    if not existing_job:
        await update_job_status(
            request.job_id, "queued", 0, "Job queued",
            {"userId": request.user_id, "type": "separation", "originalStoragePath": request.storage_path},
        )

    if get_active_job_count() >= settings.MAX_CONCURRENT_JOBS * 2:
        raise HTTPException(status_code=503, detail="Server busy, please try again shortly")

    # Mark job status in Firestore
    active_now = get_active_job_count()
    if active_now > 0:
        await update_job_status(
            request.job_id, "queued", 1,
            f"Queued: Waiting for previous separation to finish (#{active_now} active)..."
        )
    else:
        await update_job_status(
            request.job_id, "processing", 3,
            "Job accepted, starting separation worker..."
        )

    # Dispatch to background thread
    model_name = request.model or settings.DEMUCS_MODEL
    if not GPU_AVAILABLE and model_name == "htdemucs_ft":
        logger.info("Using the faster 'htdemucs' model for CPU separation")
        model_name = "htdemucs"

    dispatch_separation(
        job_id=request.job_id,
        user_id=request.user_id,
        storage_path=request.storage_path,
        model=model_name,
    )

    logger.info(f"Separation dispatched: {request.job_id}")
    return {"success": True, "job_id": request.job_id, "status": "processing"}


# ── Vocal Mix ────────────────────────────────────────────────────────────────

@router.post("/mix")
async def start_vocal_mix(
    request: MixRequest,
    token_data: dict = Depends(verify_firebase_token),
):
    """Enqueue a vocal mix job."""
    _validate_job_id(request.job_id)
    if token_data.get("uid") != request.user_id:
        raise HTTPException(status_code=403, detail="User ID mismatch")

    if not request.vocal_path or not request.instrumental_path:
        raise HTTPException(status_code=400, detail="vocal_path and instrumental_path are required")
    _assert_owned_storage_path(request.vocal_path, request.user_id)
    _assert_owned_storage_path(request.instrumental_path, request.user_id)

    existing_job = await get_job(request.job_id)
    if existing_job and existing_job.get("userId") != token_data.get("uid"):
        raise HTTPException(status_code=403, detail="Job does not belong to this user")
    if existing_job and existing_job.get("status") in {"processing", "separating", "mixing", "completed"}:
        raise HTTPException(status_code=409, detail="This job has already been submitted")
    if not existing_job:
        await update_job_status(
            request.job_id, "queued", 0, "Mix queued",
            {"userId": request.user_id, "type": "vocal_mix", "vocalStoragePath": request.vocal_path,
             "instrumentalStoragePath": request.instrumental_path},
        )

    await update_job_status(
        request.job_id, "processing", 3,
        "Mix job accepted, downloading audio..."
    )

    dispatch_vocal_mix(
        job_id=request.job_id,
        user_id=request.user_id,
        vocal_path=request.vocal_path,
        instrumental_path=request.instrumental_path,
        vocal_gain=request.vocal_gain,
        instrumental_gain=request.instrumental_gain,
        time_stretch=request.time_stretch,
    )

    logger.info(f"Vocal mix dispatched: {request.job_id}")
    return {"success": True, "job_id": request.job_id, "status": "processing"}


# ── Job Status ───────────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    token_data: dict = Depends(verify_firebase_token),
):
    """Get current job status from Firestore."""
    _validate_job_id(job_id)
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    # Verify ownership
    if job.get("userId") != token_data.get("uid"):
        raise HTTPException(status_code=403, detail="Access denied")

    return {"success": True, "job": job}


@router.get("/jobs/{job_id}/download/{asset}")
async def download_job_asset(
    job_id: str,
    asset: str,
    token_data: dict = Depends(verify_firebase_token),
):
    """Issue a fresh short-lived URL for a completed stem or mix."""
    _validate_job_id(job_id)
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("userId") != token_data.get("uid"):
        raise HTTPException(status_code=403, detail="Access denied")
    if job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="Output is not ready")

    storage_path = None
    for stem in job.get("stems", []):
        if stem.get("stem") == asset:
            storage_path = stem.get("storagePath")
            break
    if asset == "mixed" and not storage_path:
        storage_path = job.get("mixedTrackPath")
    if not storage_path:
        raise HTTPException(status_code=404, detail="Asset not found")

    from app.services.firebase_service import get_signed_url
    return {"success": True, "downloadUrl": get_signed_url(storage_path)}


@router.get("/jobs/user/{user_id}")
async def get_user_job_list(
    user_id: str,
    token_data: dict = Depends(verify_firebase_token),
):
    """Get all jobs for a user."""
    if token_data.get("uid") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    jobs = await get_user_jobs(user_id)
    return {"success": True, "jobs": jobs, "count": len(jobs)}


@router.delete("/jobs/{job_id}")
async def cancel_job_endpoint(
    job_id: str,
    token_data: dict = Depends(verify_firebase_token),
):
    """Cancel a running job."""
    _validate_job_id(job_id)
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("userId") != token_data.get("uid"):
        raise HTTPException(status_code=403, detail="Access denied")

    cancelled = cancel_job(job_id)
    if cancelled:
        await update_job_status(job_id, "failed", 0, "Cancelled by user",
                                {"error": "Cancelled by user"})

    return {"success": True, "cancelled": cancelled}


# ── Audio Analysis ───────────────────────────────────────────────────────────

@router.post("/analyze")
async def analyze_audio_endpoint(
    request: AnalyzeRequest,
    token_data: dict = Depends(verify_firebase_token),
):
    """Analyze audio for BPM, key, beats, etc."""
    import tempfile
    from app.services.firebase_service import download_from_storage

    _assert_owned_storage_path(request.storage_path, token_data.get("uid", ""))

    ext = os.path.splitext(request.storage_path)[1] or ".mp3"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await download_from_storage(request.storage_path, tmp_path)
        result = analyze_audio(tmp_path)
        return {"success": True, "analysis": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Analysis failed: {e}")
        raise HTTPException(status_code=500, detail="Audio analysis failed")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
