# AI Video Studio

A local-first workspace for planning, generating, and rendering video with LTX-2.3 MLX and Remotion. The app runs on localhost and stores project and job metadata in SQLite. Model weights and generated media stay in your configured local folders.

## Phase 1 and Phase 2

The studio includes a dark dashboard, benchmarked generation presets, a persistent SQLite job queue, local LTX-2.3 MLX and Remotion adapters, integration status, resource metrics, and a read-only importer for the Scorpion & Frog project. Imported images and videos stay in their original folders and are served locally for reference selection, preview, and ffprobe verification. Job output is streamed into the history log panel.

LTX runs with Hugging Face offline mode enabled. If the configured Q8 weights are not already cached, the API blocks a generation job rather than downloading model files. The LTX adapter uses the installed long-form CLI (`--width`, `--height`, `--frames`, `--output`); image-to-video conditioning anchors the reference with `--image <path> 0 1.0`. Every invocation runs in its own process group, so cancellation stops its Python launcher and descendants without targeting unrelated Python or Remotion processes. Outputs are written as `.partial.mp4`, checked with ffprobe for expected dimensions, frame rate, and frame count, then atomically renamed on success.

The Scorpion & Frog project has a moonlit riverbank prompt default. Project-specific generation settings are stored under a project key and take precedence; changing the project default does not overwrite those settings. Preflight shows advisory warnings for high swap use, sustained memory pressure, or low disk space. Thresholds and sample cadence are adjustable with `PREFLIGHT_MAX_SWAP_GB`, `PREFLIGHT_MIN_DISK_GB`, `PREFLIGHT_PRESSURE_FREE_PERCENT_MAX`, `PREFLIGHT_PRESSURE_CONSECUTIVE_SAMPLES`, and `PREFLIGHT_PRESSURE_SAMPLE_INTERVAL_SECONDS` in `.env`.

No GPU work starts during app setup or tests; generation begins only after a user submits the form. The queue has one worker. A canceled, failed, or interrupted run cannot be recorded as completed; any retained partial output has an explicit partial/canceled filename and is shown in the job details.

## Local setup

1. Copy `.env.example` to `.env` and review the local paths. The settings loader reads this root `.env` file.
2. Start the API:

   ```bash
   cd backend
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python -m app.server
   ```

3. In another terminal, start the frontend:

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

The API binds to `127.0.0.1` by default. See `.env.example` for adapter paths. No model weights are downloaded or copied.

Open the frontend at `http://127.0.0.1:5173`. To run backend checks, use `cd backend && pytest -q`; to build the frontend, use `cd frontend && npm run build`.

## Safety and storage

Outputs, reference images, caches, credentials, local databases, and common video/model-weight files are ignored by Git. The API validates configured paths against allowed local roots before using them. Jobs persist across restarts; interrupted jobs are returned to the queue as pending.

## Planned areas

Storyboard editing and shot-to-generation workflows remain planned. The importer indexes supported images and videos up to 2,000 assets and does not move or copy project files.
