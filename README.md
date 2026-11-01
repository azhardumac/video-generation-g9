# AI Video Studio

A local-first video creation workspace for Apple Silicon. AI Video Studio uses [LTX-2.3 MLX](https://github.com/dgrauet/ltx-2-mlx) to generate video clips and [Remotion](https://www.remotion.dev/) to assemble and export edited videos. It provides a React/TypeScript frontend, a FastAPI backend, and SQLite-backed project and job management.

Model files, project data, reference images, and generated videos are stored locally, not in this Git repository.

## Features

- Text-to-video and image-to-video generation with configurable resolution, frame count, frame rate, seed, and reference images.
- Project-scoped references, storyboards, visual style settings, and scene-continuity information.
- Saved generation jobs with progress, logs, cancellation, retry, and output previews.
- Technical output verification using FFprobe, including frame count, dimensions, and frame rate.
- Separate creative review of generated clips against selected reference images.
- Film-plan editing, verified-shot reconciliation, and Remotion export with captions and transitions.
- A first-run setup interface for local dependencies and model-download readiness.

**Limitations:** Technical verification does not establish visual quality or character consistency. Temporal image guides are experimental; video extension and IC-LoRA are not enabled by default in the supported local workflow. Some planning and creative-review steps require user input. The current application should not be assumed to turn an arbitrary prompt into a finished, automatically quality-checked film without intervention.

## Requirements

- An Apple Silicon Mac running macOS.
- Apple Command Line Tools and enough free storage for dependencies, model weights, temporary files, and output media.
- An internet connection for the initial installation and any explicitly approved model downloads.
- A local LTX-2.3 MLX Q8 installation for video generation.

The bootstrap handles installation of the project's supported Python, Node.js, FFmpeg, LTX CLI, backend, frontend, and Remotion dependencies. Model downloads can be large; disk-space checks and explicit consent are required. Performance and memory requirements vary with output settings and the model.

## Installation

Install Apple Command Line Tools if they are not already installed:

```bash
xcode-select --install
```

Clone the repository and run the setup script:

```bash
mkdir -p "$HOME/AI-Video-Studio/apps"
git clone https://github.com/azhardumac/video-generation-g9.git "$HOME/AI-Video-Studio/apps/ai-video-studio"
cd "$HOME/AI-Video-Studio/apps/ai-video-studio"
./scripts/bootstrap.sh
```

The setup installs or verifies required dependencies and creates the local configuration file if one does not exist. It preserves existing installations, settings, databases, model caches, and generated media. Missing LTX model weights are downloaded **only after explicit confirmation**. If the model repository requires access approval, accept its terms on Hugging Face and authenticate locally before retrying.

To inspect setup without installing anything:

```bash
./scripts/bootstrap.sh --check
./scripts/bootstrap.sh --dry-run
```

### Start the application

Run these commands in separate terminals from the repository root:

Backend:

```bash
./scripts/start-backend.sh
```

Frontend:

```bash
./scripts/start-frontend.sh
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). The local API uses [http://127.0.0.1:8000](http://127.0.0.1:8000) by default.

The **Setup** screen displays dependency and model availability and provides an explicit path for installing missing components.

## Using AI Video Studio

1. Create or select a project.
2. Write a video prompt or create a storyboard/film plan, and optionally choose reference images.
3. Check the selected model, generation settings, and planned workload before submitting.
4. Monitor progress and review the resulting clips. FFprobe verification confirms media integrity; creative approval is a separate decision.
5. For a multi-shot project, prepare a composition from eligible verified clips and confirm the Remotion render to export the final video.

Existing completed outputs can be reconciled to their matching shots without re-running generation. Generation and rendering are separate operations; neither should start merely from opening a project or previewing a plan.

## Configuration

The bootstrap creates `.env` from [`.env.example`](.env.example) when needed. Common settings include:

| Setting | Purpose |
| --- | --- |
| `LTX_ROOT`, `LTX_EXECUTABLE` | Local LTX MLX source and command |
| `HF_CACHE` | Hugging Face model cache |
| `REMOTION_STUDIO`, `REMOTION_EXECUTABLE` | Local Remotion runtime |
| `REMOTION_RENDER_PORT` | Optional render port; `0` selects an available port |
| `OUTPUT_ROOT` | Generated video and composition outputs |
| `DATABASE_PATH` | SQLite application database |
| `ALLOWED_PATH_ROOTS` | Directories the application may access |

See [`.env.example`](.env.example) for the full configuration and defaults. Keep `.env` and any local credentials out of version control.

## Development and checks

With dependencies installed:

```bash
cd backend
.venv/bin/python -m pytest -q
cd ../frontend
npm run build
npm run typecheck:remotion
```

To validate the setup script without installing packages or downloading models:

```bash
bash -n scripts/bootstrap.sh
./scripts/bootstrap.sh --dry-run
```

These checks do not require LTX video generation or a Remotion render.

## Architecture

| Component | Technology | Responsibility |
| --- | --- | --- |
| Frontend | React, TypeScript, Vite | Project management, generation controls, previews, review, and export |
| Backend | FastAPI, Python | API, settings, validation, job orchestration, and media serving |
| Persistence | SQLite | Projects, storyboards, revisions, jobs, and logs |
| Video generation | LTX-2.3 MLX Q8 | Local text/image-conditioned video inference |
| Editing/export | Remotion | Composition, captions, transitions, and final rendering |
| Verification | FFmpeg / FFprobe | Video metadata and output validation |

The backend processes local generation jobs sequentially and records their status. Generated files are verified before being marked complete. The export flow uses a composition manifest to assemble eligible clips and then runs an explicitly confirmed render.

## Data and privacy

AI Video Studio runs on localhost by default. Reference images, generated videos, project databases, downloaded model weights, and caches are excluded from Git. You control which project assets are imported or linked. The setup process does not upload local production data to this repository.

Dependencies and model downloads access their respective external package and hosting services during installation. Review those providers' licenses and terms before use or redistribution.

## License

Consult the repository's license file, if present, and the separate licenses governing LTX, model weights, Remotion, and other dependencies.

# Visual continuity and review (Issue #21)

Open **Scene continuity** for the selected project to choose the original frog
and scorpion references, optionally link and separately approve an environment
plate, and record per-scene layout, framing, expected character count, river
scale, camera orientation, and continuity notes. Saving creates a new project
revision and clears approval; approval is explicit. Historical outputs and
`riverbank-master`-style files are rejected as implicit anchors and require the
explicit historical-image checkbox before they can be saved as the selected
environment plate. No files are moved, replaced, or generated by this page.

The Story to Film page shows **ffprobe technical verification** separately from
creative review. For a current verified shot, compare the playable clip beside
the project's selected reference images. Approvals require every manual review
checkbox; rejections require a reason. Creative rejection preserves the clip,
job status, technical verification, and export availability. A review becomes
stale if the selected job, storyboard revision, or approved continuity revision
changes.

The safe one-shot path is to make and review a storyboard, save its shot prompt,
approve the project continuity revision, then use **Queue this shot only** on a
single shot. This action confirms the exact shot and never starts/resumes a film
batch. Independent I2V is the default. Experimental repeated image anchors can
be selected per scene and are described as temporal guidance only; they do not
lock identity or guarantee consistent characters. Video extension and IC-LoRA
controls remain disabled until local audio/memory validation and required cached
weights are available. See [the installed runtime audit](docs/LOCAL_LTX_2_3_CAPABILITY_AUDIT.md).

Backend tests do not invoke LTX or Remotion. This release does not run GPU
generation, video continuation, model downloads, or a Remotion render.


## Prompt-first orchestration (Issue #25 · Milestones A–B)

The **Create Film** landing page records project-scoped prompts, approved image
references, explicit generation authorization, output/time limits, and resumable
stage checkpoints in SQLite. Request keys are idempotent; the browser retains a
keyed prompt fingerprint across refresh without storing the prompt in local
storage, and repeated submissions with the same key and payload return the same
run. A read-only preview uses
only matching benchmark history and verified project output sizes. If no matching
measurement exists, it says so instead of inventing a cost estimate. An interrupted
run resumes only from a persisted paused checkpoint; it does not re-submit queued
LTX jobs automatically. Existing independent local queue jobs retain their
pre-existing worker behavior.

An optional local structured planner can be configured using
`STORY_PLANNER_COMMAND` in `.env` as a JSON argument array, for example
`["/absolute/path/to/local-planner"]`. The executable receives a JSON request
and schema on stdin and must return one `StoryPlan` JSON object on stdout. The
app never starts a shell, calls a cloud provider, or downloads planner weights.
Without this executable, Create Film saves the authorized run and reports that
planning is blocked; it creates no storyboard or generation jobs. A configured
command is called **unverified** until its output passes the contract and
project-reference checks for that run.

Validated plans persist project-scoped characters, identity traits, locations,
ordered scenes and shots, framing, continuity rules, and linked reference
provenance. One authorization advances the run to the existing single-worker
LTX queue. Queue creation is idempotent per film run; job timeouts enforce the
remaining generation-time budget, and completed output sizes gate subsequent
shots. One image can condition each shot; text-only shots are supported, and
image conditioning does not guarantee cross-shot character identity. Technical
ffprobe verification stays separate from creative approval. Generated clips
stop in **creative review pending**; this milestone does not automatically
approve visuals, repair shots, assemble a manifest, or run Remotion.
