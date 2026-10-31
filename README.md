# AI Video Studio

Private, local-first AI video studio for generating portrait video with **LTX-2.3 MLX** and assembling projects with **Remotion**. React + TypeScript + Vite frontend, FastAPI backend, and SQLite job/project metadata. Everything runs on localhost; model weights, reference assets, and generated media remain on the workstation.

## Current status (31 October 2026)

**Phases 1, 2, 2.5, 3, 3.1, 3.2, and Issue #11 Milestones A, B, and C are integrated on `main`.** The project lifecycle and production workspace provide:

- Real local projects with create, open/switch, rename, archive/restore, and read-only import for allowed directories.
- Project-scoped local reference links and uploads. References are explicitly selected, categorized, validated, editable, and removable without deleting source files.
- Persisted active-project selection, per-project settings/jobs/assets/storyboards, and genuinely blank storyboards. New projects do not import or link historical media or story content.

Milestone B adds project-specific story text and a revisioned visual style/character bible; editable per-shot framing, prompt review, reference, seed, frame preset, dimensions, frame rate and output path; stale/verified output state; project-scoped queued shot jobs; local composition preview; and safe output reveal. A new project remains empty until the user writes its story and explicitly selects references. LTX supports exactly one conditioning image per shot, and the review panel exposes the full prompt assembled from the saved style bible and shot text.

Milestone C removes the legacy standalone Remotion form with hardcoded Scorpion & Frog composition/output defaults; rendering remains available from the reviewed storyboard assembly flow. Generation summaries now reflect active project settings and preset, controls have visible keyboard focus and accessible mobile navigation labels, and a clean-production regression verifies empty initial data plus only the two explicitly selected approved character references. No LTX generation or Remotion render was executed during Issue #11 work.

The application is not declared fully operational based on code tests alone. Real generation and Remotion rendering require explicit user action, and this Story-to-Film flow has not been run on the GPU or rendered as part of implementation.

The `main` foundation includes:

- Responsive dark dashboard; local integration health for LTX, the offline Q8 model cache, Remotion, the story project, and ffprobe.
- Short / Standard / Extended / Experimental generation presets; project-scoped prompt/settings; reference-image selection, seed, and output location.
- Persistent SQLite-backed single-worker job queue, job logs, retries and cancellation. LTX and Remotion subprocesses use isolated process groups to stop their descendants on cancellation.
- Project importer (read-only), asset references, local video previews, benchmark history, elapsed-time and resource metrics.
- Offline Hugging Face inference. Output is first written as `.partial.mp4`; ffprobe checks dimensions, frame rate and counted frames before it is finalized.
- Advisory resource warnings (not hard generation blocks). The 193-frame option warns of observed visual drift.
- Revisioned SQLite storyboards with scene groups, editable/reorderable shots, local reference paths, per-shot presets/seeds/captions, job links, logs, retries, and cancellation.
- Deterministic Remotion manifest generation from only completed, ffprobe-verified portrait 24 fps outputs. The timeline preview plays verified clips in storyboard order; the app-owned Remotion composition applies crossfades and optional captions.
- Remotion rendering is queued only after the user confirms in the storyboard UI. Render output is ffprobe-checked for the timeline's portrait dimensions, 24 fps, and exact frame count.

**Verified end-to-end:** A Scorpion & Frog image-to-video job completed in the application on 2026-10-31: **49 frames, 24 fps, 448 × 768, 184.36 seconds, exit code 0**, ffprobe-verified and playable in the frontend. Reported VAE peak Metal memory: **7.58 GB**. The SQLite job record and video remain on the local workstation, not in Git.

**Tests reported from local runs:** 23 backend tests passed and the frontend production build passed after Phase 2.5 responsive-dashboard fixes. The repository does not run GPU generation in CI or during installation.

**Phase 3.1 validation:** 33 backend tests passed and the frontend production build passed. Tests cover settings-snapshot staleness, jobs edited while queued/running, safe attachment of an existing ffprobe-verified clip, and the one-shot manifest edge case. On the reference Mac, the existing Scorpion & Frog 49-frame clip was explicitly attached to its storyboard shot, re-probed at 448 × 768/24 fps/49 frames, and resolved into a one-shot manifest. The packaged Remotion composition was discovered with the installed local CLI and configured `--public-dir` on a separate port. No video was rendered; the first real Remotion render still requires explicit user confirmation in the app.

**Phase 3.2 finding:** the Remotion renderer previously defaulted to port 3000, which was occupied by the unrelated `npx remotion studio` process. The app now selects an ephemeral loopback port by default; set `REMOTION_RENDER_PORT` to a free fixed port to override it. Failed render jobs retain final CLI output for diagnosis and identify render type/duration in the UI.

The Phase 3.2 adapter regressions pass with the backend suite; the frontend production build and packaged Remotion type-check also pass. Dynamic-port composition discovery succeeded on the reference Mac. The existing failed render was not retried.

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

   Set the paths for `LTX_ROOT`, `LTX_EXECUTABLE`, `HF_CACHE`, `REMOTION_STUDIO`, `REMOTION_EXECUTABLE`, `STORY_PROJECT`, `OUTPUT_ROOT`, and `ALLOWED_PATH_ROOTS` to your existing installations. The configuration points to existing files and does not copy or redownload them. `REMOTION_RENDER_PORT=0` chooses an available dynamic port; set it to a specific free port if needed. The renderer passes that port to Remotion and does not assume port 3000 is available.

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
2. Create or open a project in **Projects**. Link only reference images explicitly approved for that production. A new storyboard starts empty.
3. In **Generate video**, write the prompt, choose a preset, and click Generate **once**. Only this explicit submission starts GPU work.
4. Check **Job queue** and **History & Logs** for live output and resource readings. On success, the finalized MP4 is playable in the UI and includes ffprobe verification.
5. A failed or canceled job is not marked completed; partial media is retained with an explicit filename if present.

The LTX adapter calls the installed CLI with `--width`, `--height`, `--frames`, `--frame-rate`, `--output`, and image conditioning via `--image <reference-path> 0 1.0`, along with `--distilled --low-ram --no-audio` for the tested preset. The Hugging Face and Transformers offline environment flags are set for local jobs.

## Planning a multi-shot storyboard

1. Open **Projects** and select a project, then open **Storyboard**.
2. Enter a title and create a blank storyboard. No prompt, scene, shot, reference, or old output is inserted automatically.
3. Add scene and shot details explicitly, select a reference image, preset, seed, and caption. Use the up/down controls to order shots, then select **Save storyboard**. Each save creates an ordered revision; stale revisions are rejected instead of overwriting newer edits.
4. Select **Queue shot** on each shot you intend to generate. This is the explicit action that submits one shot to the existing single-worker LTX queue. Generation does not run during import, editing, tests, manifest preview, or app startup. Follow progress, logs, cancellation, retry and the verified video preview on each shot card.
5. To reuse a prior clip without generating again, select it under **Reuse verified generation**, choose **Use selected job settings**, save the new storyboard revision, then choose **Attach existing clip**. This adds a link to the existing job; it does not copy or change the source video. The app re-runs ffprobe and requires the prompt, reference image, seed, and preset to match. If any of those shot settings later change, the prior clip is marked stale and cannot enter a new manifest; its job and video remain available as history.
6. After every shot is completed and ffprobe-verified, select **Preview verified timeline**. The app produces a deterministic manifest under `OUTPUT_ROOT/storyboards/<storyboard-id>/composition-manifest.json` and plays the clips in order for review. Clips must remain under `OUTPUT_ROOT`, use matching portrait dimensions, and be 24 fps; missing, stale, unverified, mismatched, or moved files block assembly.
7. Review the timeline, crossfade markers, captions and total duration. **Confirm & queue Remotion render** asks for confirmation before adding a render job. The packaged `remotion/` composition uses the existing local Remotion CLI and its installed packages without editing the existing Remotion project. The rendered MP4 is stored under `OUTPUT_ROOT/storyboards/<storyboard-id>/` and verified before completion. No render is started by import, clip attachment, or preview.

Shot deletion is a soft delete and preserves revision snapshots and job/video history. The app blocks removing or archiving shots with queued/running jobs; cancel those jobs first. Archiving a storyboard also retains its database history and media files. Crossfades default to eight frames, and captions are optional text overlays. The storyboard timeline preview is a local playback preview; final transitions/caption compositing are applied by Remotion when its render is explicitly confirmed.

## Story-to-Film: Scorpion & Frog claymation

This workflow creates a new storyboard and keeps any existing woodland draft or historical media untouched.

1. In the project switcher, open **Scorpion & Frog — Claymation**, then choose **Story to Film**.
2. Select the linked original frog and scorpion character images. The page displays both previews and linked paths. If a file is missing, restore it or relink the original under **References**, then choose **Recheck project data**.
3. Enter or select the story. **Load the approved river-crossing fable** inserts the exact requested plot into the form; it does not save until you choose **Build Film Plan**. Choose target format, LTX preset, and optional seed.
4. **Build Film Plan** creates a separate six-scene, twelve-shot storyboard with saved shot prompts, scene order, visual descriptions, framing, dialogue/captions, timing estimates, transitions, references, and validated local generation settings. No jobs are queued. The planner is deterministic because this installation has no configured local planning model; it accepts only the approved frog/scorpion river-crossing fable and rejects other story text rather than inventing plot events.
5. LTX accepts one conditioning image per shot. Each shot therefore attaches one selected character image, while the plan records both characters when they share a scene. The app does not create or imply a two-image composite. River and water are required by this story; if the existing style bible forbids them, **Build Film Plan** creates a new style-bible revision that permits the river while retaining the previous revision and continuing to forbid reuse of `riverbank-master.png`, old videos, benchmark media, and old storyboard material.
6. Review all scene and shot cards. Edit prompts or dialogue/captions and choose **Save plan edits**; saving creates a new storyboard revision and invalidates approval. Tick the review statement and choose **Approve Film Plan** to record approval for the exact storyboard and style-bible revisions. Approval alone does not queue work.
7. **Generate Film** explicitly queues all incomplete shots in order. The existing local worker runs one job at a time. The page shows persisted status, actual job logs, resource warnings, verified output previews, and ffprobe width, height, frame rate, and frame count. Pause prevents the next batch shot from starting (a running shot may finish); cancel stops queued work and requests cancellation of the active child process. Resume continues a paused batch. Selective retry queues only checked failed/cancelled shots; verified completed shots are never duplicated.
8. Open the always-visible **Export / Remotion** navigation item. It reports missing shots, verified source batches, reconciliation state, manifest readiness, and local Remotion availability even when no composition manifest exists. Choose **Reconcile Verified Shots** to select a complete source batch; the app re-runs ffprobe against every clip and links them to the exact approved shot revision. Only matching queued duplicates in paused same-revision batches are cancelled. Completed extra outputs are retained. This recovery operation never starts LTX.
9. The **Export / Remotion** panel has one progressive primary action. If the manifest is absent, **Prepare composition** writes a deterministic manifest under `OUTPUT_ROOT/storyboards/<storyboard-id>/composition-manifest.json`; this metadata-only operation reuses the clips in saved shot order and does not encode a video. If a complete source batch needs recovery, open **Technical recovery and batch history**, choose **Reconcile Verified Shots**, then return to **Prepare composition**.
10. When the manifest is ready, **Render final film** asks for confirmation before starting the Remotion render. The page then shows saved render progress and disables repeat render actions. When complete, the ffprobe-verified MP4 is playable in the page and **Watch / Reveal final MP4** opens its output location. A compact status ribbon shows source clips, composition, and final export state. Prior duplicate batches and their logs stay in collapsed history. Generate Film remains idempotent for reconciled clips and will not create another batch when all shots are ready.

For the already generated 12-shot river-crossing film, select its saved plan and open **Export / Remotion**. The ribbon reports 12/12 source clips, composition status, and final render status based on the persisted current shot links and render job. If recovery is needed, use **Technical recovery and batch history** to reconcile; prior cancelled duplicate batches stay collapsed and completed extra clips remain in history. Click **Prepare composition** if the manifest is absent; the status will report 548 frames, 22.83 seconds, 448×768 at 24 fps. Then click **Render final film** and confirm. This uses the 12 existing verified clips; it does not run LTX. After rendering, refreshes and restarts restore progress and the completed MP4 status from SQLite job history.

Generation model, local output location, seed defaults, distilled mode, and low-RAM mode remain project-scoped. The story's content supplies each shot prompt; an unrelated saved prompt is not appended to or substituted for the approved fable. The `Experimental` 193-frame preset retains its visual-drift warning. All created media remains under configured local output paths and outside Git.

## Tests

```bash
cd ~/AI-Video-Studio/apps/ai-video-studio/backend
source .venv/bin/activate
python -m pytest -q

cd ../frontend
npm run build
npm run typecheck:remotion
```

The Remotion check type-checks only this repository's `remotion/` composition against the existing installation named by `REMOTION_STUDIO` (or `~/AI-Video-Studio/remotion-studio`). It does not edit that installation or render a video. Tests and builds do not require a GPU generation run.

## Data handling and safeguards

- `.env`, secrets, SQLite databases, videos, reference images, model weights, downloads, and caches are excluded from Git.
- Imported project assets stay in their original locations.
- Uploaded reference images are copied only after explicit file selection and stored in app-owned project reference storage. The upload limit defaults to 20 MiB (`PROJECT_UPLOAD_MAX_BYTES`). Removing a reference removes its database link and retains the file.
- Local path validation restricts file access to configured roots.
- One GPU generation job runs at a time; output is verified before job completion.
- Process-group cancellation terminates the launched job and its children, not unrelated local applications.
- Resource warning defaults (customizable in `.env`): swap above 24 GB; free disk below 10 GB; memory-free percentage at or below 15% across three samples 10 seconds apart. These are **advisory thresholds**, not validated hardware requirements.
- Before launching any additional GPU work, inspect the selected prompt, frame preset and reference image.

## Roadmap

**Next production milestone:** exercise the Story-to-Film workflow with a user-approved local LTX batch and, after clip verification, one separately approved Remotion render. Fix any machine-specific issues without replacing existing project assets or history.

## Repository workflow

`main` is the integration branch for completed phases. Feature work belongs on `feature/*` branches with PRs targeting `main`. Phase 1, Phase 2, and Phase 2.5 PRs were integrated without force pushes. The GitHub repository is private.
