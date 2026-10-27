# StemAI — AI Music Separation Platform

An AI audio application with four-stem separation, BPM/key analysis, vocal alignment, instrumental reconstruction, and WAV/320 kbps MP3 delivery.

## Deployment model

Vercel serves the static frontend only. The FastAPI/Demucs API must run as a separate, persistent Docker service; the included `render.yaml` is a Render Blueprint starter, not a service that has already been deployed. Firebase Auth, Firestore, and Cloud Storage provide authentication and durable metadata/audio.

## Local setup

Requires Python 3.10–3.12, FFmpeg/ffprobe, PyTorch-compatible hardware, and disk/RAM for WAV intermediates. CPU processing can be slow; a supported NVIDIA GPU is recommended for faster separation. On first job, Demucs downloads model weights; model files are intentionally excluded.

```sh
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# The backend verifies Firebase ID tokens for all users, including anonymous
# guest sessions. Configure a Firebase Admin service account locally or through
# your shell environment; never commit the credential.
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In `frontend/.env`, set the `VITE_FIREBASE_*` values from your Firebase web app config and leave `VITE_API_URL` blank for local Vite development; the dev proxy sends `/api` calls to `http://localhost:8000`. Run the frontend with `cd frontend && npm ci && npm run dev`. In production, set `VITE_API_URL` to the backend's public origin and rebuild the frontend.

For local Docker, run `docker compose up --build` (CPU). On a Linux host with NVIDIA Container Toolkit installed, use `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build` and check `/api/health` for `gpu_available: true`.

## Firebase production setup

1. Enable Email/Password, Google, and Anonymous Authentication providers, then create Firestore/Storage.
2. Deploy `firestore.rules` and `storage.rules` with Firebase CLI.
3. Mount a least-privilege Firebase service account as a host secret and set `GOOGLE_APPLICATION_CREDENTIALS` (or set the complete JSON in the secret `FIREBASE_SERVICE_ACCOUNT_JSON`) plus `FIREBASE_STORAGE_BUCKET`. Never commit the key. The Admin SDK verifies Firebase ID tokens.
4. Deploy `frontend/` to Vercel and the API container separately; configure the API URL, exact allowed browser origins, and the service-account secret. The API verifies signed Firebase ID tokens.

The Firebase Web API key is a public client identifier; Firebase Admin credentials are server secrets and must never be committed. Guest sessions use Firebase Anonymous Auth, so the backend can verify their identity like any other user. Temporary Demucs work files are removed on success; failed job files should be covered by host retention/cleanup policies.

## API

- `GET /api/health` — API status, configured model, CPU/GPU device and worker count.
- `POST /api/upload` — multipart fields `file`, `user_id`, and optional `folder`.
- `POST /api/separate` — JSON job request with `job_id`, `user_id`, `storage_path`, and model.
- `GET /api/jobs/{id}` — authenticated job status, progress, analysis and outputs.
- `GET /api/jobs/{id}/download/{asset}` — authenticated download URL for an output stem.
- `POST /api/mix` and `POST /api/analyze` — vocal mixing and audio analysis.

The upload has actual browser progress, validates format/size, and shows API errors. Job stages move through queued, processing, separating/mixing, then completed/failed. Outputs are only marked complete after files are generated, verified, and stored.

## Processing details and limitations

`htdemucs` produces vocals/drums/bass/other. The instrumental is the sum of drums, bass, and other, not EQ or center-channel cancellation. Separation jobs use a short, downsampled excerpt for optional BPM/key metadata so full-track analysis does not delay the AI job. The first separation may also download model weights. CPU separation remains hardware-bound; no software change can make a CPU match a GPU.

This uses an in-process job runner plus Firestore status persistence. Run one API process per instance; in-memory execution cannot resume a job after a host crash. Production horizontal scaling/crash durability needs a managed task queue (Cloud Tasks/Celery/Redis) and lease recovery. Configure request timeouts and disk cleanup appropriate to your host. Backend upload limit: 250 MB; the frontend currently limits files to 200 MB.

## Deploy the backend on Render

1. Upload/push this cleaned project to a GitHub repository. Do not include `.env` files, service-account JSON files, model weights, or `node_modules`.
2. In Render, choose **New → Blueprint**, connect the repository, and select its root `render.yaml`. The Blueprint builds the Docker service from `backend/`.
3. In the backend service's Environment settings, add `FIREBASE_SERVICE_ACCOUNT_JSON` using a newly generated Firebase Admin service account JSON as a secret. Also set `FIREBASE_STORAGE_BUCKET` and `FIREBASE_PROJECT_ID` to your Firebase project. Never put the Admin JSON in Vercel or in this ZIP.
4. Wait for the Render service to finish deploying. Open `https://<render-service>.onrender.com/api/health`; it must return JSON with `status: "ok"`. Check `device` and `gpu_available` to see whether the host has a usable GPU.
5. Import the GitHub repository into Vercel and set its **Root Directory** to `frontend`; Vercel will detect Vite and build the frontend package.
6. In Vercel, open **Project → Settings → Environment Variables** and set `VITE_API_URL` to `https://<render-service>.onrender.com` (origin only: no `/api`, no trailing slash). Add all six `VITE_FIREBASE_*` client configuration values from Firebase Project Settings → General → Your apps. These are build-time variables; the Firebase web API key is public client configuration, not the Admin credential.
7. Update Render's `CORS_ORIGINS` to the exact Vercel production origin (including `https://` and no trailing slash), then redeploy the backend. Redeploy the Vercel frontend after setting its variables.
8. In the app, Settings should report the API as online and show the backend's device/model. Test an upload, then a short separation. If uploads fail, inspect the browser Network tab for `/api/upload`; if separation is slow, compare the displayed CPU/GPU status and inspect backend logs.

The included Render template allows the production Vercel origin plus local Vite origins. If you use a different frontend domain, update `CORS_ORIGINS` to that exact origin and redeploy the backend. The included service configuration does not request a GPU, so check the health response; choose a GPU-capable host or plan if separation speed is a priority.

## Separation speed

The default is single-pass `htdemucs`; the backend caches its model between jobs, limits CPU concurrency by default, and uses lower-cost analysis for job metadata. These avoidable delays are reduced, but the Demucs model itself is compute-heavy. A 3-minute track taking 40 minutes is consistent with a weak or overloaded CPU and cannot be confirmed or fixed to a specific time without the machine's hardware. For substantially faster results, run the backend on a supported NVIDIA GPU and confirm `/api/health` reports `gpu_available: true`. If it reports CPU, increasing thread count may help some machines but does not substitute for a GPU.

The repository does not include Firebase Admin credentials, Demucs weights, or GPU resources. Run the verification checklist below on the deployed API with credentials and a representative track before accepting production traffic.

## Verification checklist

1. `GET /api/health` reports the expected model and GPU state.
2. Sign in from the frontend, upload an MP3/WAV/FLAC/M4A file, and confirm the backend returns a storage path.
3. Start a separation and observe `queued → processing → separating → completed` in the UI.
4. Play and download every returned stem; verify the files are non-empty and have distinct audio content.
5. Record or upload a vocal, upload an instrumental, and verify the mixer reports BPMs and produces a playable 320 kbps MP3.
6. Retry a bad file and confirm the UI shows the backend error and the job is `failed`, never falsely `completed`.

