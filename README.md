# AI Video Studio

A local-first workspace for planning, generating, and rendering video with LTX-2.3 MLX and Remotion. The app runs on localhost and stores project and job metadata in SQLite. Model weights and generated media stay in your configured local folders.

## Phase 1

Phase 1 provides the dark studio shell, generation presets, settings, a persistent local job API, and configurable adapter boundaries. Generation is never launched during setup or startup. The LTX adapter is invoked only when a user starts a generation job.

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

## Safety and storage

Outputs, reference images, caches, credentials, and the SQLite database are ignored by Git. The API validates configured paths against allowed local roots before using them. Jobs persist across restarts; interrupted jobs are returned to the queue as pending.

## Planned areas

Dashboard, projects, storyboard and shot planning, reference library, generation, queue, history and logs, Remotion rendering, and settings are represented in the studio shell. Additional workflow depth will land in subsequent phases.
