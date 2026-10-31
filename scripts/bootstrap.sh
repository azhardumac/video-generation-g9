#!/usr/bin/env bash
set -Eeuo pipefail

readonly LTX_REPOSITORY="https://github.com/dgrauet/ltx-2-mlx.git"
readonly LTX_REVISION="14a3ddd6054736beb45b4d836135fa4d44490531"
readonly PYTHON_PIN="3.11.17"
readonly NODE_MAJOR="22"
readonly REMOTION_PIN="4.0.538"
readonly REMOTION_REACT_PIN="19.2.3"
readonly MODEL_ID="dgrauet/ltx-2.3-mlx-q8"
readonly APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly PYTHON_HELPER="$APP_ROOT/scripts/bootstrap_support.py"
readonly MODEL_HELPER="$APP_ROOT/scripts/cache_ltx_model.py"

dotenv_value() {
  local key="$1" value
  [[ -f "$APP_INSTALL_DIR/.env" ]] || return 0
  value="$(awk -v key="$key" 'index($0,key"=")==1 {sub(/^[^=]*=/,""); sub(/\r$/,""); print; exit}' "$APP_INSTALL_DIR/.env")"
  if [[ "$value" == '"'*'"' || "$value" == "'*'" ]]; then value="${value:1:${#value}-2}"; fi
  printf '%s' "$value"
}

expand_home() {
  case "$1" in
    '~') printf '%s' "$HOME" ;;
    '~/'*) printf '%s/%s' "$HOME" "${1#~/}" ;;
    *) printf '%s' "$1" ;;
  esac
}

APP_INSTALL_DIR="${AI_VIDEO_STUDIO_APP_DIR:-$APP_ROOT}"
if [[ -z "${AI_VIDEO_STUDIO_HOME:-}" ]]; then
  allowed_roots_from_env="$(dotenv_value ALLOWED_PATH_ROOTS)"
  if [[ -n "$allowed_roots_from_env" ]]; then AI_VIDEO_STUDIO_HOME="${allowed_roots_from_env%%,*}"; fi
fi
AI_VIDEO_STUDIO_HOME="$(expand_home "${AI_VIDEO_STUDIO_HOME:-$HOME/AI-Video-Studio}")"
LTX_ROOT="$(expand_home "${LTX_ROOT:-$(dotenv_value LTX_ROOT)}")"
HF_CACHE="$(expand_home "${HF_CACHE:-${HF_HOME:-$(dotenv_value HF_CACHE)}}")"
REMOTION_STUDIO="$(expand_home "${REMOTION_STUDIO:-$(dotenv_value REMOTION_STUDIO)}")"
STORY_PROJECT="$(expand_home "${STORY_PROJECT:-$(dotenv_value STORY_PROJECT)}")"
OUTPUT_ROOT="$(expand_home "${OUTPUT_ROOT:-$(dotenv_value OUTPUT_ROOT)}")"
LTX_ROOT="${LTX_ROOT:-$AI_VIDEO_STUDIO_HOME/src/ltx-2-mlx}"
HF_CACHE="${HF_CACHE:-$AI_VIDEO_STUDIO_HOME/models/ltx/huggingface}"
REMOTION_STUDIO="${REMOTION_STUDIO:-$AI_VIDEO_STUDIO_HOME/remotion-studio}"
STORY_PROJECT="${STORY_PROJECT:-$AI_VIDEO_STUDIO_HOME/projects/scorpion-frog}"
OUTPUT_ROOT="${OUTPUT_ROOT:-$AI_VIDEO_STUDIO_HOME/outputs}"
SETUP_STATE_DIR="$AI_VIDEO_STUDIO_HOME/.setup-state"
MODE="install"
MODEL_CHOICE="ask"

owns_path() {
  local marker="$SETUP_STATE_DIR/$1"
  [[ -f "$marker" ]] && [[ "$(cat "$marker")" == "$2" ]]
}

mark_path() {
  local marker="$SETUP_STATE_DIR/$1"
  mkdir -p "$SETUP_STATE_DIR"
  if [[ ! -f "$marker" ]]; then (umask 077; printf '%s' "$2" > "$marker"); fi
}

is_empty_directory() {
  [[ -d "$1" ]] && [[ -z "$(find "$1" -mindepth 1 -maxdepth 1 -print -quit)" ]]
}

usage() {
  cat <<'USAGE'
Usage: ./scripts/bootstrap.sh [--check | --dry-run | --install-missing] [--download-model | --skip-model]

Default is an idempotent install of missing pinned dependencies. Model weights
are never downloaded automatically: an interactive terminal asks for consent,
or pass --download-model only after the user has explicitly approved it.
--skip-model is used by the app's setup wizard when model download is unchecked.
USAGE
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
stage() { printf 'SETUP_STAGE:%s\n' "$*"; }

while (($#)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --check) MODE="check" ;;
    --dry-run) MODE="dry-run" ;;
    --install-missing) MODE="install" ;;
    --download-model) MODEL_CHOICE="yes" ;;
    --skip-model) MODEL_CHOICE="no" ;;
    *) fail "Unknown argument: $1" ;;
  esac
  shift
done

check_command() { command -v "$1" >/dev/null 2>&1; }
version_line() { "$@" 2>&1 | head -n 1 || true; }
check_line() {
  local name="$1" status="$2" detail="$3"
  printf 'CHECK %s %s %s\n' "$name" "$status" "$detail"
}

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  fail "Supported setup target is Apple Silicon macOS (arm64). Detected $(uname -s) $(uname -m)."
fi

if [[ "$MODE" == "check" ]]; then
  for tool in brew git gh ffmpeg ffprobe uv node npm; do
    if check_command "$tool"; then
      check_line "$tool" installed "$(version_line "$(command -v "$tool")" --version)"
    else
      check_line "$tool" missing "not found on PATH"
    fi
  done
  printf 'CHECK python %s %s\n' "$(python3.11 --version 2>&1 || true)" "$PYTHON_PIN"
  if [[ -d "$LTX_ROOT/.git" ]]; then
    revision="$(git -C "$LTX_ROOT" rev-parse HEAD 2>/dev/null || true)"
    [[ "$revision" == "$LTX_REVISION" ]] && check_line ltx_source installed "$revision" || check_line ltx_source incompatible "$revision expected $LTX_REVISION"
  else check_line ltx_source missing "$LTX_ROOT"; fi
  [[ -x "$LTX_ROOT/.venv/bin/ltx-2-mlx" ]] && check_line ltx_runner installed "$LTX_ROOT/.venv/bin/ltx-2-mlx" || check_line ltx_runner missing "$LTX_ROOT/.venv/bin/ltx-2-mlx"
  if [[ -x "$REMOTION_STUDIO/node_modules/.bin/remotion" ]]; then check_line remotion installed "$REMOTION_STUDIO/node_modules/.bin/remotion $REMOTION_PIN"; else check_line remotion missing "$REMOTION_STUDIO"; fi
  [[ -f "$APP_INSTALL_DIR/.env" ]] && check_line env installed "preserved existing .env" || check_line env missing "created only if absent"
  printf 'CHECK model opt-in-required %s under %s\n' "$MODEL_ID" "$HF_CACHE"
  exit 0
fi

if [[ "$MODE" == "dry-run" ]]; then
  printf '%s\n' "Would install Homebrew formulas git gh ffmpeg python@3.11 uv node@22."
  printf '%s\n' "Would create the FastAPI venv, install locked npm dependencies, install exact Remotion $REMOTION_PIN, pin LTX $LTX_REVISION, and create directories."
  printf '%s\n' "Would preserve .env/databases/references/outputs and skip $MODEL_ID unless explicitly approved."
  exit 0
fi

free_kib="$(df -Pk "$APP_ROOT" | awk 'NR==2 {print $4}')"
[[ "$free_kib" =~ ^[0-9]+$ ]] || fail "Could not determine free space on the application volume."
if ((free_kib < 12 * 1024 * 1024)); then
  fail "At least 12 GiB free is recommended for application dependencies; $(awk -v k="$free_kib" 'BEGIN {printf "%.1f GiB", k/1024/1024}') is available. Free space and rerun."
fi

if ! xcode-select -p >/dev/null 2>&1; then
  xcode-select --install >/dev/null 2>&1 || true
  fail "Install the Apple Command Line Tools from the prompt, then rerun this command."
fi

if ! check_command brew; then
  command -v curl >/dev/null || fail "curl is required to install Homebrew."
  stage "homebrew:Installing Homebrew"
  installer="$(mktemp "${TMPDIR:-/tmp}/homebrew-install.XXXXXX")"
  trap 'rm -f "$installer"' EXIT
  curl --fail --location --silent --show-error https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh --output "$installer"
  NONINTERACTIVE=1 /bin/bash "$installer" || fail "Homebrew install did not complete. Review the terminal error and rerun; partial Homebrew setup is safe to resume."
  rm -f "$installer"
  trap - EXIT
fi

if [[ -x /opt/homebrew/bin/brew ]]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
else
  eval "$(brew shellenv)"
fi
brew_prefix="$(brew --prefix)"
formulas=(git gh ffmpeg python@3.11 uv node@22)
for formula in "${formulas[@]}"; do
  if brew list --versions "$formula" >/dev/null 2>&1; then
    printf 'SETUP_STAGE:brew:%s already installed\n' "$formula"
  else
    stage "brew:Installing $formula"
    brew install "$formula"
  fi
done

python_bin="$brew_prefix/opt/python@3.11/bin/python3.11"
[[ -x "$python_bin" ]] || python_bin="$(command -v python3.11 || true)"
[[ -x "$python_bin" ]] || fail "Homebrew Python 3.11 is missing after installation."
node_bin="$brew_prefix/opt/node@22/bin/node"
[[ -x "$node_bin" ]] || node_bin="$(command -v node || true)"
[[ -x "$node_bin" ]] || fail "Node.js 22 is missing after installation."
export PATH="$brew_prefix/opt/python@3.11/bin:$brew_prefix/opt/node@22/bin:$brew_prefix/bin:$PATH"
[[ "$("$node_bin" -p 'process.versions.node.split(".")[0]')" == "$NODE_MAJOR" ]] || fail "Expected Node.js $NODE_MAJOR LTS; detected $("$node_bin" --version)."
[[ "$("$python_bin" -c 'import sys; print(".".join(map(str,sys.version_info[:2])))')" == "3.11" ]] || fail "Expected Python 3.11; detected $("$python_bin" --version)."

stage "directories:Creating configured directories"
remotion_path_existed=0
[[ -e "$REMOTION_STUDIO" ]] && remotion_path_existed=1
mkdir -p "$AI_VIDEO_STUDIO_HOME/apps" "$AI_VIDEO_STUDIO_HOME/src" "$AI_VIDEO_STUDIO_HOME/models/ltx" \
  "$HF_CACHE" "$REMOTION_STUDIO" "$STORY_PROJECT" "$OUTPUT_ROOT" "$APP_INSTALL_DIR/data"

# Never replace an existing Remotion project/package manifest. An incompatible
# existing install is reported for manual review rather than silently upgraded.
remotion_cli="$REMOTION_STUDIO/node_modules/.bin/remotion"
remotion_existing_exact=0
if ((remotion_path_existed)); then
  if owns_path remotion-path "$REMOTION_STUDIO" || is_empty_directory "$REMOTION_STUDIO"; then
    mark_path remotion-path "$REMOTION_STUDIO"
  else
    if [[ -x "$remotion_cli" ]]; then
      installed_remotion="$(node -e 'for(const p of ["@remotion/cli","remotion","react","react-dom"]){try{process.stdout.write(require(process.argv[1]+"/node_modules/"+p+"/package.json").version+" ")}catch{process.stdout.write("missing ")}}' "$REMOTION_STUDIO" 2>/dev/null || true)"
      if [[ "$installed_remotion" == "$REMOTION_PIN $REMOTION_PIN $REMOTION_REACT_PIN $REMOTION_REACT_PIN " ]]; then
        remotion_existing_exact=1
      else
        fail "Existing Remotion installation is incompatible ($installed_remotion). It was not changed. Set REMOTION_STUDIO to a new, separate install directory to continue."
      fi
    else
      fail "Remotion directory already contains user data but has no verified pinned runtime. It was not changed. Set REMOTION_STUDIO to a new, separate install directory to continue."
    fi
  fi
else
  mark_path remotion-path "$REMOTION_STUDIO"
fi

if [[ ! -e "$REMOTION_STUDIO/package.json" ]]; then
  owns_path remotion-path "$REMOTION_STUDIO" || fail "Refusing to create a manifest inside an existing Remotion directory."
  cp "$APP_ROOT/scripts/remotion-runtime/package.json" "$REMOTION_STUDIO/package.json"
  cp "$APP_ROOT/scripts/remotion-runtime/package-lock.json" "$REMOTION_STUDIO/package-lock.json"
fi
if [[ -d "$REMOTION_STUDIO/node_modules" ]]; then
  installed_remotion="$(node -e 'for(const p of ["@remotion/cli","remotion"]){try{process.stdout.write(require(process.argv[1]+"/node_modules/"+p+"/package.json").version+" ")}catch{process.stdout.write("missing ")}}' "$REMOTION_STUDIO" 2>/dev/null || true)"
  for installed_version in $installed_remotion; do
    if [[ "$installed_version" != "missing" && "$installed_version" != "$REMOTION_PIN" ]]; then
      fail "Existing Remotion installation is incompatible ($installed_remotion). It was not changed. Configure REMOTION_STUDIO to a new empty directory or resolve it manually."
    fi
  done
fi

stage "python:Installing tested Python $PYTHON_PIN"
uv python install "$PYTHON_PIN"

# Create config once. Helper uses O_EXCL and writes mode 0600; a manually edited
# .env is never parsed by a shell or replaced by this script.
values_file="$(mktemp "${TMPDIR:-/tmp}/studio-env-values.XXXXXX.json")"
trap 'rm -f "$values_file"' EXIT
AI_VIDEO_STUDIO_HOME="$AI_VIDEO_STUDIO_HOME" LTX_ROOT="$LTX_ROOT" HF_CACHE="$HF_CACHE" \
REMOTION_STUDIO="$REMOTION_STUDIO" STORY_PROJECT="$STORY_PROJECT" OUTPUT_ROOT="$OUTPUT_ROOT" \
APP_INSTALL_DIR="$APP_INSTALL_DIR" "$python_bin" - "$values_file" <<'PY'
import json, os, sys
values = {
 "LTX_ROOT":os.environ["LTX_ROOT"], "HF_CACHE":os.environ["HF_CACHE"],
 "REMOTION_STUDIO":os.environ["REMOTION_STUDIO"], "STORY_PROJECT":os.environ["STORY_PROJECT"],
 "LTX_EXECUTABLE":os.path.join(os.environ["LTX_ROOT"], ".venv/bin/ltx-2-mlx"),
 "REMOTION_EXECUTABLE":os.path.join(os.environ["REMOTION_STUDIO"], "node_modules/.bin/remotion"),
 "OUTPUT_ROOT":os.environ["OUTPUT_ROOT"], "ALLOWED_PATH_ROOTS":os.environ["AI_VIDEO_STUDIO_HOME"],
}
with open(sys.argv[1], "w") as stream: json.dump(values, stream)
PY
"$python_bin" "$PYTHON_HELPER" --write-env-if-missing --template "$APP_INSTALL_DIR/.env.example" \
  --destination "$APP_INSTALL_DIR/.env" --values-json "$values_file"
rm -f "$values_file"
trap - EXIT

# Pin the local runner source. Existing trees are never reset, cleaned or checked
# out to another revision; mismatches stop with an actionable message.
ltx_path_existed=0
[[ -e "$LTX_ROOT" ]] && ltx_path_existed=1
if [[ -e "$LTX_ROOT" ]]; then
  [[ -d "$LTX_ROOT/.git" ]] || fail "LTX path exists but is not a complete Git checkout; it was left untouched: $LTX_ROOT"
  if [[ -n "$(git -C "$LTX_ROOT" status --porcelain)" ]]; then
    fail "Pinned LTX checkout has local edits; it was not modified. Review them or set LTX_ROOT to a separate install directory."
  fi
  ltx_revision="$(git -C "$LTX_ROOT" rev-parse HEAD)"
  if [[ "$ltx_revision" != "$LTX_REVISION" ]]; then
    if owns_path ltx-path "$LTX_ROOT"; then
      stage "ltx_source:Resuming pinned source checkout"
      git -C "$LTX_ROOT" fetch origin "$LTX_REVISION"
      git -C "$LTX_ROOT" checkout --detach "$LTX_REVISION"
    else
      fail "Existing LTX checkout is at $ltx_revision, expected $LTX_REVISION; it was not modified. Set LTX_ROOT to a separate install directory to install the pinned runner."
    fi
  fi
else
  stage "ltx_source:Cloning pinned LTX source"
  mark_path ltx-path "$LTX_ROOT"
  git clone "$LTX_REPOSITORY" "$LTX_ROOT"
  git -C "$LTX_ROOT" checkout --detach "$LTX_REVISION"
fi
ltx_cli="$LTX_ROOT/.venv/bin/ltx-2-mlx"
ltx_python="$LTX_ROOT/.venv/bin/python"
if owns_path ltx-path "$LTX_ROOT"; then
  stage "ltx_dependencies:Syncing locked LTX dependencies"
  uv sync --project "$LTX_ROOT" --locked --all-extras --python "$PYTHON_PIN"
elif ((ltx_path_existed)); then
  [[ -x "$ltx_cli" && -x "$ltx_python" ]] || fail "Existing LTX source has no verified runner environment and was not modified. Set LTX_ROOT to a separate install directory to install the pinned runner."
  "$ltx_python" -c 'import importlib.metadata as m; v=m.version("ltx-pipelines-mlx"); assert v == "0.18.0", v' \
    || fail "Existing LTX environment is not ltx-pipelines-mlx 0.18.0 and was not modified. Set LTX_ROOT to a separate install directory."
  printf '%s\n' "Existing pinned LTX source/runtime verified; its virtual environment was preserved."
else
  fail "Pinned LTX source installation state is unavailable; no LTX files were modified."
fi
[[ -x "$ltx_cli" ]] || fail "LTX CLI entrypoint is missing after the locked environment sync."
"$ltx_python" -c 'import importlib.metadata as m; v=m.version("ltx-pipelines-mlx"); print("ltx-pipelines-mlx",v); assert v == "0.18.0"' \
  || fail "Pinned LTX package version verification failed. The CLI is not executed because its import initializes Metal."

stage "backend:Syncing locked FastAPI environment"
UV_PROJECT_ENVIRONMENT="$APP_INSTALL_DIR/backend/.venv" uv sync --project "$APP_INSTALL_DIR/backend" --locked --no-dev --python "$PYTHON_PIN"
"$APP_INSTALL_DIR/backend/.venv/bin/python" -c 'import fastapi,uvicorn,pydantic; print(f"FastAPI {fastapi.__version__}, Uvicorn {uvicorn.__version__}, Pydantic {pydantic.__version__}")'

stage "frontend:Installing npm lockfile dependencies"
npm --prefix "$APP_INSTALL_DIR/frontend" ci --no-audit --no-fund

if [[ ! -x "$remotion_cli" && "$remotion_existing_exact" != 1 ]]; then
  stage "remotion:Installing locked Remotion $REMOTION_PIN dependency tree"
  REMOTION_SKIP_CHROMIUM_DOWNLOAD=1 npm --prefix "$REMOTION_STUDIO" ci --no-audit --no-fund --ignore-scripts
fi
[[ -x "$remotion_cli" ]] || fail "Remotion CLI is missing after installation."
remotion_version="$(node -p "require('$REMOTION_STUDIO/node_modules/@remotion/cli/package.json').version")"
[[ "$remotion_version" == "$REMOTION_PIN" ]] || fail "Remotion version mismatch: expected $REMOTION_PIN, found $remotion_version. Existing files were retained."

stage "directories:Verifying application directories"
mkdir -p "$APP_INSTALL_DIR/data" "$OUTPUT_ROOT" "$STORY_PROJECT" "$HF_CACHE"

if [[ -x "$LTX_ROOT/.venv/bin/python" ]]; then
  if [[ "$MODEL_CHOICE" == "ask" && -t 0 ]]; then
    printf 'The model cache can require tens of gigabytes: %s\n' "$MODEL_ID"
    read -r -p "Download missing Q8 model files now? (y/N) " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]] && MODEL_CHOICE="yes" || MODEL_CHOICE="no"
  fi
  if [[ "$MODEL_CHOICE" == "yes" ]]; then
    stage "model:Starting explicitly approved Q8 model preflight"
    HF_HOME="$HF_CACHE" uv run --project "$LTX_ROOT" --locked --python "$PYTHON_PIN" \
      "$MODEL_HELPER" --cache-dir "$HF_CACHE" --download
  else
    stage "model:Skipping model download; explicit approval was not provided"
    HF_HOME="$HF_CACHE" HF_HUB_OFFLINE=1 uv run --project "$LTX_ROOT" --locked --python "$PYTHON_PIN" \
      "$MODEL_HELPER" --cache-dir "$HF_CACHE" --check || model_status=$?
    if [[ "${model_status:-0}" -eq 2 ]]; then printf '%s\n' "Model missing; app will remain local/offline and generation stays unavailable until explicit opt-in setup."; fi
  fi
fi

if [[ -f "$APP_INSTALL_DIR/.env" ]]; then printf '%s\n' "Local .env exists and was preserved."; fi
printf '%s\n' "Bootstrap complete. Start the app with the documented local startup commands. No generation or Remotion render was run."
