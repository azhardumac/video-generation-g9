# AI Video Studio

A local-first workspace for planning, generating, and rendering video with LTX-2.3 MLX and Remotion. The app runs on localhost and stores project and job metadata in SQLite. Model weights and generated media stay in your configured local folders.

## Phase 1 and Phase 2

The studio includes a dark dashboard, benchmarked generation presets, a persistent SQLite job queue, local LTX-2.3 MLX and Remotion adapters, integration status, resource metrics, and a read-only importer for the Scorpion & Frog project. Imported images and videos stay in their original folders and are served locally for reference selection, preview, and ffprobe verification. Job output is streamed into the history log panel.

LTX runs with Hugging Face offline mode enabled. If the configured Q8 weights are not already cached, the API blocks a generation job rather than downloading model files. No GPU work starts during app setup or tests; generation begins only after a user submits the form. The queue has one worker.

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

Outputs, reference images, caches, credentials, and the SQLite database are ignored by Git. The API validates configured paths against allowed local roots before using them. Jobs persist across restarts; interrupted jobs are returned to the queue as pending.

## Planned areas

Storyboard editing and shot-to-generation workflows remain planned. The importer indexes supported images and videos up to 2,000 assets and does not move or copy project files.
