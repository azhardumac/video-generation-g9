# Local LTX-2.3 MLX Q8 capability audit

Audit date: 2026-10-31. This is a read-only audit of the installed runner at
`~/AI-Video-Studio/src/ltx-2-mlx` and its existing Hugging Face cache. No model
was downloaded, and no inference was run.

## Installation observed

- Installed repository commit: `14a3ddd6054736beb45b4d836135fa4d44490531`.
- Installed `ltx-pipelines-mlx`: `0.18.0`.
- Configured Q8 repository exists in `~/AI-Video-Studio/models/ltx/huggingface/hub/models--dgrauet--ltx-2.3-mlx-q8`; the cache occupies about 82 GB on disk and includes the Q8 model assets. The existing Gemma 3 12B 4-bit text-encoder cache is also present.
- No IC-LoRA model repository is present in the inspected Hugging Face cache. This project must remain offline; do not fetch gated weights or duplicate cached model files.
- The checkout is clean. The actual command-line parser is in `packages/ltx-pipelines-mlx/src/ltx_pipelines_mlx/cli.py`.

## Command help limitation

Invoking even `ltx-2-mlx --version` or a `--help` subcommand in this headless
Codex session fails during package import with `ImportError: [metal::load_device]
No Metal device available`. The inspection therefore used the installed parser,
pipeline source, and its colocated `PIPELINES.md` / `PIPELINE_MATURITY.md`, not an
upstream-only feature list. A native terminal session with Metal access can run
the help command later. This audit did not make a GPU call.

## Locally supported paths

| Path | Installed support | Local readiness and limits |
|---|---|---|
| `generate --distilled` T2V/I2V | CLI and pipeline present; accepts `--prompt`, `--model`, `--width`, `--height`, `--frames` / `-f`, mandatory `--frame-rate`, `--output`, `--seed`, `--distilled`, `--low-ram`, and `--no-audio`. Output extension in the parser is MP4. | The app's existing 49-frame I2V path and resulting clip are the local proven baseline. Two-stage generation is also exposed by `--two-stage`; use Q8 local weights and the existing offline HF cache. `--low-ram` streams transformer blocks and is documented as reducing transformer peak memory by about 75%; this is not a guarantee that every VAE, image decode, or system allocation fits 16 GB. The earlier successful app generation reported 7.58 GB VAE peak Metal memory. |
| Repeated `generate --image` | Supported in the parser. Each occurrence takes `PATH [FRAME_IDX STRENGTH ...]`; anchors refer to 0-based temporal frames and can guide multiple points in one clip. | Experimental for the studio's multi-anchor workflow. It does not provide per-character segmentation, spatial masks, or an identity lock. An image at each temporal index may guide the frame there; it does not guarantee a frog and scorpion remain unique or stable between anchors. The UI records anchors only when individually selected and the scene package is approved. |
| Two-image interpolation / guide mode | The installed docs/source describe `generate --two-stage` with two image guides and `--refine-audio` as the replacement for the removed `keyframe` command. The CLI accepts `--image PATH FRAME_IDX STRENGTH [BINDING]` on 2.3 packs. | The app's proven workflow uses `--distilled`, not this CFG two-stage interpolation path. The alternate interpolation mode has not been run on this local Q8/M1 Pro configuration; it also denoises audio in stage 2 (the app output mux can be disabled, but the compute path differs). Treat it as an unvalidated experiment, not a ready control. Distilled start/end interpolation in local docs is specifically benchmarked on 2.5 packs and is not claimed for this 2.3 cache. |
| `generate --segment` | Supported; repeated prompts are assigned to timeline slices in latent frames. | Prompt Relay changes temporal prompt conditioning, not object count, framing, or character placement. Treat it as an experimental creative aid, not a continuity fix. |
| `retake` | Installed `[beta]` subcommand with source video and latent start/end window. `--low-ram` exists; `--no-regen-audio` preserves source audio. | It can regenerate a time window, but docs warn the new portion may not blend cleanly at the boundaries. `retake --distilled` is experimental. A workflow preserving no-audio and verified output has not been validated on this M1 Pro 16 GB setup. It is not offered as a working Studio action. |
| `extend` | Installed `[beta]` subcommand; accepts `--video`, `--extend-frames`, `--direction`, `--low-ram`; `--distilled` and repeated `--image` are marked experimental. | Distilled extension uses the source clip's last 25 frames as context and can accept an image for newly appended frames. It does not guarantee permanent character identity. The local maturity examples validate LTX-2.5 Q8 on M1 Max, not this cached LTX-2.3 Q8 / M1 Pro 16 GB setup. `extend` has no `--no-audio` flag. Keep disabled pending a separately approved, no-audio and memory-safe validation. |
| `ic-lora` | Installed subcommand accepts one or more `--lora PATH STRENGTH` and required `--video-conditioning PATH STRENGTH`, with optional `--low-ram`. | Required conditioning weights are not in the local cache. It requires compatible control-video input (for example pose/depth/canny) and is not a character-reference identity lock by itself. No downloads or UI action are offered. |

## 16 GB M1 Pro safeguards

The installed `--low-ram` mode is the right baseline for the existing Q8
generation profile, but its documented memory reduction applies to transformer
block residency. It does not certify peak memory for source-video decode,
conditioning, VAE decode, two simultaneous image identities, or the operating
system. Keep only one local generation worker active, check swap/memory pressure
and disk space before queueing, use the verified Short preset for initial
validation, and stop on pressure warnings. No inference was run as part of this
audit.

## Studio feature gate

- Safe default: one independently conditioned image-to-video shot or text-to-video
  shot through the existing worker.
- Experimental option: explicitly approved, repeated temporal image anchors for a
  single shot only. The option is disclosed as temporal guidance, not an identity
  or scene-consistency guarantee.
- Disabled: video retake/extension and IC-LoRA generation. The former needs local
  LTX-2.3 and audio/memory qualification; the latter also needs separately
  obtained control weights, which are absent.
- Creative acceptance remains a user review. `ffprobe` status is technical only.
