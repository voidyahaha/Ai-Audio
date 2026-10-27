"""
Demucs stem separation service.
Uses HTDemucs/HTDemucs FT for highest quality separation.
Never pretends to have separated anything — verifies output files exist before reporting success.
"""

import os
import threading
import torch
from loguru import logger
from typing import Callable, Optional, Dict, List
import time

from app.config import settings

# Detect hardware
_requested_device = settings.DEMUCS_DEVICE.lower()
if _requested_device == "auto":
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
elif _requested_device == "cuda" and not torch.cuda.is_available():
    logger.warning("DEMUCS_DEVICE=cuda requested but CUDA is unavailable; falling back to CPU")
    DEVICE = "cpu"
elif _requested_device in {"cpu", "cuda"}:
    DEVICE = _requested_device
else:
    raise ValueError("DEMUCS_DEVICE must be set to auto, cpu, or cuda")
GPU_AVAILABLE = DEVICE == "cuda" and torch.cuda.is_available()
CPU_THREADS = max(1, int(settings.DEMUCS_CPU_THREADS))

if DEVICE == "cpu":
    torch.set_num_threads(CPU_THREADS)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        # PyTorch only permits setting inter-op threads once per process.
        pass

logger.info(f"Demucs: device={DEVICE}, GPU={GPU_AVAILABLE}")
logger.info(f"Demucs CPU threads: {CPU_THREADS}")

_MODEL_CACHE = {}
_MODEL_CACHE_LOCK = threading.Lock()

STEM_MAP = {
    "htdemucs": ["vocals", "drums", "bass", "other"],
    "htdemucs_ft": ["vocals", "drums", "bass", "other"],
    "mdx_extra": ["vocals", "drums", "bass", "other"],
    "htdemucs_6s": ["vocals", "drums", "bass", "other", "piano", "guitar"],
}

def get_available_stems(model_name: str) -> List[str]:
    return STEM_MAP.get(model_name, ["vocals", "drums", "bass", "other"])

def separate_audio(
    input_path: str,
    output_dir: str,
    model_name: str = "htdemucs",
    on_progress: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, str]:
    """
    Separate audio into stems using Demucs.

    Args:
        input_path: Path to input audio file
        output_dir: Directory to save separated stems
        model_name: Demucs model name
        on_progress: Callback(percent, message)

    Returns:
        Dict mapping stem name -> output file path
    """
    from demucs.apply import apply_model, BagOfModels
    from demucs.audio import AudioFile, save_audio
    from demucs.pretrained import get_model

    # Keep CPU jobs on the faster four-stem model.
    if DEVICE == "cpu" and model_name == "htdemucs_ft":
        logger.info("Auto-selecting fast 'htdemucs' model for CPU processing")
        model_name = "htdemucs"

    logger.info(f"Starting separation: model={model_name}, device={DEVICE}")
    logger.info(f"Input: {input_path}")
    logger.info(f"Output dir: {output_dir}")

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    os.makedirs(output_dir, exist_ok=True)

    start_time = time.time()

    try:
        # Load model
        if on_progress:
            on_progress(18, f"Initializing AI model ({model_name})...")

        model_load_start = time.time()
        cached_model = False
        with _MODEL_CACHE_LOCK:
            model = _MODEL_CACHE.get(model_name)
            if model is None:
                logger.info(f"Loading Demucs model: {model_name}")
                model = get_model(model_name)
                model.to(DEVICE)
                model.eval()
                _MODEL_CACHE[model_name] = model
            else:
                cached_model = True
        logger.info(
            f"Model ready in {time.time() - model_load_start:.1f}s "
            f"(cached={cached_model})"
        )

        if on_progress:
            on_progress(22, "Preparing audio for separation...")

        # Load audio
        wav = AudioFile(input_path).read(
            streams=0,
            samplerate=model.samplerate,
            channels=model.audio_channels,
        )

        # wav shape: (channels, samples)
        ref = wav.mean(0)
        # A silent or nearly-silent file has zero variance.  Do not allow it
        # to create NaNs that make Demucs fail with an opaque model error.
        ref_std = ref.std().clamp_min(1e-8)
        wav = (wav - ref.mean()) / ref_std
        wav = wav.unsqueeze(0)  # Add batch dim: (1, channels, samples)

        total_samples = wav.shape[-1]
        logger.info(f"Audio loaded: shape={wav.shape}, sr={model.samplerate}")

        if on_progress:
            on_progress(25, "Separating stems with AI (25%)...")

        last_reported_pct = [25]
        last_reported_time = [time.time()]
        def chunk_progress_callback(d):
            offset = d.get("segment_offset", 0)
            model_idx = d.get("model_idx_in_bag", 0)
            num_models = d.get("models", 1)
            chunk_ratio = min(1.0, max(0.0, offset / max(1, total_samples)))
            overall_ratio = (model_idx + chunk_ratio) / max(1, num_models)
            pct = int(25 + overall_ratio * 55)
            now = time.time()
            # Frequent progress updates (every 1% if at least 2s elapsed) so UI moves smoothly
            if pct > last_reported_pct[0] and (now - last_reported_time[0] >= 2.0 or pct >= last_reported_pct[0] + 2):
                last_reported_pct[0] = pct
                last_reported_time[0] = now
                if on_progress:
                    on_progress(pct, f"Separating audio with AI ({pct}%)...")

        # CPU inference uses one pass and no overlap to reduce latency.
        shifts = 0 if DEVICE == "cpu" else settings.DEMUCS_SHIFTS
        overlap = 0.0 if DEVICE == "cpu" else settings.DEMUCS_OVERLAP

        inference_start = time.time()
        with torch.inference_mode():
            if isinstance(model, BagOfModels):
                sources = apply_model(
                    model,
                    wav,
                    device=DEVICE,
                    shifts=shifts,
                    split=True,
                    overlap=overlap,
                    progress=False,
                    num_workers=0,
                    callback=chunk_progress_callback,
                )
            else:
                sources = apply_model(
                    model,
                    wav,
                    device=DEVICE,
                    shifts=shifts,
                    split=True,
                    overlap=overlap,
                    progress=False,
                    callback=chunk_progress_callback,
                )

        logger.info(
            f"Model inference completed in {time.time() - inference_start:.1f}s "
            f"(total so far {time.time() - start_time:.1f}s)"
        )

        if on_progress:
            on_progress(80, "Saving stem files...")

        # Denormalize
        sources = sources * ref_std + ref.mean()

        # Save each stem
        stem_names = model.sources
        output_files = {}

        for i, stem_name in enumerate(stem_names):
            stem_audio = sources[0, i]  # Remove batch dim
            output_path = os.path.join(output_dir, f"{stem_name}.wav")

            save_audio(
                stem_audio.cpu(),
                output_path,
                samplerate=model.samplerate,
            )

            if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
                raise RuntimeError(f"Stem file missing or empty: {output_path}")

            output_files[stem_name] = output_path
            logger.info(f"Saved stem: {stem_name} -> {output_path} ({os.path.getsize(output_path)//1024}KB)")

        # Generate instrumental = sum of all non-vocal stems
        if "vocals" in output_files and len(output_files) > 1:
            if on_progress:
                on_progress(88, "Generating instrumental mix...")

            non_vocal_sources = [
                sources[0, i]
                for i, name in enumerate(stem_names)
                if name != "vocals"
            ]
            if non_vocal_sources:
                instrumental = torch.stack(non_vocal_sources).sum(0)
                instrumental_path = os.path.join(output_dir, "instrumental.wav")
                save_audio(instrumental.cpu(), instrumental_path, samplerate=model.samplerate)
                output_files["instrumental"] = instrumental_path
                logger.info(f"Generated instrumental: {instrumental_path}")

        if on_progress:
            on_progress(95, "Stems ready, uploading...")

        total_time = time.time() - start_time
        logger.info(f"Separation complete in {total_time:.1f}s. Files: {list(output_files.keys())}")

        return output_files

    except Exception as e:
        logger.exception(f"Separation failed: {e}")
        raise RuntimeError(f"Demucs separation failed: {str(e)}")


def get_stem_info(stem_path: str) -> Dict:
    """Get audio file metadata."""
    import soundfile as sf
    try:
        info = sf.info(stem_path)
        return {
            "duration": info.duration,
            "sample_rate": info.samplerate,
            "size_bytes": os.path.getsize(stem_path),
        }
    except Exception as e:
        logger.warning(f"Could not get stem info for {stem_path}: {e}")
        return {"duration": None, "sample_rate": None, "size_bytes": os.path.getsize(stem_path)}
