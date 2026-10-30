# AI Video Studio

Private, local-first AI video studio for generating portrait video with **LTX-2.3 MLX** and assembling projects with **Remotion**. React + TypeScript + Vite frontend, FastAPI backend, and SQLite job/project metadata. Everything runs on localhost; model weights, reference assets, and generated media remain on the workstation.

## Current status (31 October 2026)

**Phases 1, 2 and 2.5 are integrated on `main`; Phase 3 is implemented on this feature branch.** The studio includes:

- Responsive dark dashboard; local integration health for LTX, the offline Q8 model cache, Remotion, the story project, and ffprobe.
- Short / Standard / Extended / Experimental generation presets; project-scoped prompt/settings; reference-image selection, seed, and output location.
- Persistent SQLite-backed single-worker job queue, job logs, retries and cancellation. LTX and Remotion subprocesses use isolated process groups to stop their descendants on cancellation.
- Project importer (read-only), asset references, local video previews, benchmark history, elapsed-time and resource metrics.
- Offline Hugging Face inference. Output is first written as `.partial.mp4`; ffprobe checks dimensions, frame rate and counted frames before it is finalized.
- Advisory resource warnings (not hard generation blocks). The 193-frame option warns of observed visual drift.
- Revisioned SQLite storyboards with scene groups, editable/reorderable shots, local reference paths, per-shot presets/seeds/captions, job links, logs, retries, and cancellation. The Scorpion & Frog importer indexes its current assets in place and seeds a riverbank opening shot.
- Deterministic Remotion manifest generation from only completed, ffprobe-verified portrait 24 fps outputs. The timeline preview plays verified clips in storyboard order; the app-owned Remotion composition applies crossfades and optional captions.
- Remotion rendering is queued only after the user confirms in the storyboard UI. Render output is ffprobe-checked for the timeline's portrait dimensions, 24 fps, and exact frame count.

**Verified end-to-end:** A Scorpion & Frog image-to-video job completed in the application on 2026-10-31: **49 frames, 24 fps, 448 × 768, 184.36 seconds, exit code 0**, ffprobe-verified and playable in the frontend. Reported VAE peak Metal memory: **7.58 GB**. The SQLite job record and video remain on the local workstation, not in Git.

**Tests reported from local runs:** 23 backend tests passed and the frontend production build passed after Phase 2.5 responsive-dashboard fixes. The repository does not run GPU generation in CI or during installation.

**Phase 3 validation on this branch:** 30 backend tests passed, including isolated API and temporary-media workflow tests; the frontend TypeScript/production build passed; and the packaged Remotion entrypoint type-checked against the installed local Remotion/React types. Tests do not start a worker, generate model output, or render a video.

## Hardware and software requirements

### Validated reference system (not a universal minimum)

- **Apple MacBook Pro with M1 Pro, 16 GB unified memory**, running macOS with Apple Metal / MLX support.
- Locally installed **LTX-2.3 MLX Q8** model (`dgrauet/ltx-2.3-mlx-q8`) and compatible `ltx-2-mlx` CLI.
- Existing Remotion Studio installation (verified with **Remotion CLI 4.0.538**).
- Python 3, Node.js/npm, and **ffprobe** available locally.
- Sufficient free disk space for cached model weights, temporary processing, swap, and generated videos. **About 40–41 GiB free** was available during integration testing; this is *observed headroom*, not a formally validated minimum.

The 16 GB M1 Pro setup is the **smallest configuration actually validated in this project**, not proof that 16 GB is required or sufficient for every model, resolution, operating system, or workload. Higher-memory machines may have more headroom. Metal memory, macOS swap, overall RAM, and disk availability are different metrics; swap can spike during initial model loading and later stabilize.

### Application prerequisites

- Python 3 with `venv` and `pip` (exact minimum Python release not yet established).
- Node.js and npm compatible with this repository's Vite 6 / TypeScript toolchain (exact minimum Node version not yet verified on the reference machine).
- `ffprobe` from FFmpeg; the exact tested version is not recorded.
- A local LTX CLI and cached model files. **The app will not download weights**.
- Remotion is needed for composition/rendering, but not for basic app navigation.

## Generation presets

| Preset | Frames | FPS | Approx. duration | Local benchmark | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| Short | 49 | 24 | 2.04 s | 200.5 s standalone; **184.36 s via app** | Verified |
| Standard (default) | 97 | 24 | 4.04 s | 326.3 s | Verified |
| Extended | 145 | 24 | 6.04 s | 449.2 s | Verified |
| Experimental | 193 | 24 | 8.04 s | 597.9 s | Verified generation; visible character/framing drift near end |

Benchmarks are from this specific machine, at 448 × 768 with the Q8 distilled low-RAM no-audio workflow. They are **not performance guarantees**. The startup memory spike is known, and the app displays advisory swap, disk and sustained memory-pressure warnings.

## Running locally

Default project checkout path: `~/AI-Video-Studio/apps/ai-video-studio`.

1. Create and review the environment configuration:

   ```bash
   cd ~/AI-Video-Studio/apps/ai-video-studio
   cp .env.example .env
   ```

   Set the paths for `LTX_ROOT`, `LTX_EXECUTABLE`, `HF_CACHE`, `REMOTION_STUDIO`, `REMOTION_EXECUTABLE`, `STORY_PROJECT`, `OUTPUT_ROOT`, and `ALLOWED_PATH_ROOTS` to your existing installations. The configuration points to existing files and does not copy or redownload them.

2. Start FastAPI in terminal 1:

   ```bash
   cd ~/AI-Video-Studio/apps/ai-video-studio/backend
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python -m app.server
   ```

3. Start Vite in terminal 2:

   ```bash
   cd ~/AI-Video-Studio/apps/ai-video-studio/frontend
   npm install
   npm run dev
   ```

Open **http://127.0.0.1:5173**. The API runs at **http://127.0.0.1:8000** by default, bound to loopback. Existing virtual environments and npm dependencies can be reused; there is no need to reinstall them on every startup.

## Generating a clip

1. Open **Settings** to check local LTX runner, model cache, ffprobe, project, and Remotion integration status.
2. Import the local Scorpion & Frog project if it is not already indexed. Select the riverbank scene reference.
3. In **Generate video**, choose **Short** (49 frames) for the first test, check the scene prompt and seed, and click Generate **once**. Only this explicit submission starts GPU work.
4. Check **Job queue** and **History & Logs** for live output and resource readings. On success, the finalized MP4 is playable in the UI and includes ffprobe verification.
5. A failed or canceled job is not marked completed; partial media is retained with an explicit filename if present.

The LTX adapter calls the installed CLI with `--width`, `--height`, `--frames`, `--frame-rate`, `--output`, and image conditioning via `--image <reference-path> 0 1.0`, along with `--distilled --low-ram --no-audio` for the tested preset. The Hugging Face and Transformers offline environment flags are set for local jobs.

## Planning a multi-shot storyboard

1. Start FastAPI and Vite as described above, then open **Storyboard** in the left navigation.
2. Choose **Import Scorpion & Frog**. The importer reads the configured `STORY_PROJECT` and creates a saved SQLite storyboard with the existing riverbank reference. It does not move or copy images or videos.
3. Edit the scene name, shot title and prompt. Choose a local reference image and one of Short, Standard, Extended or Experimental; optionally set a seed and caption. Use the up/down controls to order shots. Add shots for new scenes by changing the scene name, then select **Save storyboard**. Each save creates an ordered revision; stale revisions are rejected instead of overwriting newer edits.
4. Select **Queue shot** on each shot you intend to generate. This is the explicit action that submits one shot to the existing single-worker LTX queue. Generation does not run during import, editing, tests, manifest preview, or app startup. Follow progress, logs, cancellation, retry and the verified video preview on each shot card.
5. After every shot is completed and ffprobe-verified, select **Preview verified timeline**. The app produces a deterministic manifest under `OUTPUT_ROOT/storyboards/<storyboard-id>/composition-manifest.json` and plays the clips in order for review. Clips must remain under `OUTPUT_ROOT`, use matching portrait dimensions, and be 24 fps; missing, unverified, mismatched, or moved files block assembly.
6. Review the timeline, crossfade markers, captions and total duration. **Confirm & queue Remotion render** asks for confirmation before adding a render job. The packaged `remotion/` composition uses the existing local Remotion CLI and its installed packages without editing the existing Remotion project. The rendered MP4 is stored under `OUTPUT_ROOT/storyboards/<storyboard-id>/` and verified before completion.

Shot deletion is a soft delete and preserves revision snapshots and job/video history. The app blocks removing or archiving shots with queued/running jobs; cancel those jobs first. Archiving a storyboard also retains its database history and media files. The initial importer seeds one shot; additional scene structure and prompts are user-edited. Crossfades default to eight frames, and captions are optional text overlays. The storyboard timeline preview is a local playback preview; final transitions/caption compositing are applied by Remotion when its render is explicitly confirmed.

## Tests

```bash
cd ~/AI-Video-Studio/apps/ai-video-studio/backend
source .venv/bin/activate
python -m pytest -q

cd ../frontend
npm run build
```

Tests and builds do not require a GPU generation run.

## Data handling and safeguards

- `.env`, secrets, SQLite databases, videos, reference images, model weights, downloads, and caches are excluded from Git.
- Imported project assets stay in their original locations.
- Local path validation restricts file access to configured roots.
- One GPU generation job runs at a time; output is verified before job completion.
- Process-group cancellation terminates the launched job and its children, not unrelated local applications.
- Resource warning defaults (customizable in `.env`): swap above 24 GB; free disk below 10 GB; memory-free percentage at or below 15% across three samples 10 seconds apart. These are **advisory thresholds**, not validated hardware requirements.
- Before launching any additional GPU work, inspect the selected prompt, frame preset and reference image.

## Roadmap

**Phase 3 follow-up:** richer storyboard templates, scene-level narrative tools, and expanded transition/caption styling. Persistent storyboards, editable per-shot plans, individual LTX queue actions, and verified Remotion composition assembly are implemented in this phase. Preserve all local project media and model caches.

## Repository workflow

`main` is the integration branch for completed phases. Feature work belongs on `feature/*` branches with PRs targeting `main`. Phase 1, Phase 2, and Phase 2.5 PRs were integrated without force pushes. The GitHub repository is private.
