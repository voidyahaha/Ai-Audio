"""
StemAI Backend — FastAPI application entry point.
"""

import sys
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.config import settings
from app.services.firebase_service import init_firebase
from app.api.routes import router

# Path to built frontend
_FRONTEND_DIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "frontend", "dist"
)
_FRONTEND_DIST = os.path.normpath(_FRONTEND_DIST)


# ── Logging setup ─────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{name}</cyan> - {message}",
    level="DEBUG" if settings.DEBUG else "INFO",
    colorize=True,
)
logger.add(
    "logs/stemai.log",
    rotation="50 MB",
    retention="7 days",
    level="DEBUG",
    enqueue=True,
)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown."""
    logger.info("=" * 60)
    logger.info("StemAI Backend starting...")
    logger.info(f"  Demucs model : {settings.DEMUCS_MODEL}")
    logger.info(f"  Max jobs     : {settings.MAX_CONCURRENT_JOBS}")
    logger.info(f"  Temp dir     : {settings.PROCESSING_TEMP_DIR}")

    # Initialize Firebase
    try:
        firebase_app = init_firebase()
        logger.info(f"  Firebase     : {'✓ connected' if firebase_app else '✗ not configured'}")
    except Exception as e:
        logger.error(f"  Firebase     : ✗ {e}")
        logger.warning("  Running without Firebase — some features will fail")

    # Log GPU status
    from app.services.demucs_service import GPU_AVAILABLE, DEVICE
    logger.info(f"  Device       : {DEVICE} {'(GPU ✓)' if GPU_AVAILABLE else '(CPU)'}")
    logger.info("=" * 60)

    yield

    logger.info("StemAI Backend shutting down...")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="StemAI API",
    description="AI-powered music stem separation and vocal mixing",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — restrict browser access to explicitly configured frontend origins.
cors_origins = [
    origin.strip()
    for origin in settings.CORS_ORIGINS.split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Global exception handler ──────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled error on {request.url}: {exc}")
    return JSONResponse(
        status_code=500,
        # Exceptions can contain storage paths, provider responses, or other
        # operational detail. Keep that in server logs and return a stable,
        # safe message to clients.
        content={"success": False, "error": "Internal server error"},
    )


# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(router)


# ── Serve built React frontend or API info ──────────────────────────────────
if os.path.isdir(_FRONTEND_DIST):
    # Serve static assets (JS, CSS, images) from /assets
    app.mount("/assets", StaticFiles(directory=os.path.join(_FRONTEND_DIST, "assets")), name="assets")

    # Serve favicon
    @app.get("/favicon.svg")
    async def favicon():
        fav = os.path.join(_FRONTEND_DIST, "favicon.svg")
        if os.path.isfile(fav):
            return FileResponse(fav)
        return JSONResponse({"error": "not found"}, status_code=404)

    # Root: serve index.html for React Frontend
    @app.get("/")
    async def root_index():
        index = os.path.join(_FRONTEND_DIST, "index.html")
        if os.path.isfile(index):
            return FileResponse(index)
        return JSONResponse({"error": "Frontend not built"}, status_code=404)

    # Catch-all: serve index.html for React Router (SPA)
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Don't catch API routes or documentation as frontend routes.
        if full_path.startswith("api") or full_path.startswith("docs") or full_path.startswith("openapi") or full_path.startswith("listen"):
            return JSONResponse({"error": "not found"}, status_code=404)
        index = os.path.join(_FRONTEND_DIST, "index.html")
        if os.path.isfile(index):
            return FileResponse(index)
        return JSONResponse({"error": "Frontend not built"}, status_code=404)

    logger.info(f"Serving frontend from: {_FRONTEND_DIST}")
else:
    @app.get("/")
    async def root():
        return {
            "name": "StemAI API",
            "version": "1.0.0",
            "status": "running",
            "docs": "/docs",
        }


# ── Dev runner ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    import os
    os.makedirs("logs", exist_ok=True)
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        workers=1,         # 1 worker — Demucs is memory-heavy
        timeout_keep_alive=120,
    )
