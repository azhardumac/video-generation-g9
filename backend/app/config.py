from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    database_path: str = "./data/studio.sqlite3"
    ltx_root: str = "~/AI-Video-Studio/src/ltx-2-mlx"
    hf_cache: str = "~/AI-Video-Studio/models/ltx/huggingface"
    remotion_studio: str = "~/AI-Video-Studio/remotion-studio"
    story_project: str = "~/AI-Video-Studio/projects/scorpion-frog"
    ltx_executable: str = "~/AI-Video-Studio/src/ltx-2-mlx/.venv/bin/ltx-2-mlx"
    remotion_executable: str = "~/AI-Video-Studio/remotion-studio/node_modules/.bin/remotion"
    ffprobe_executable: str = "ffprobe"
    output_root: str = "~/AI-Video-Studio/outputs"
    allowed_path_roots: str = "~/AI-Video-Studio"
    preflight_max_swap_gb: float = Field(default=24.0, ge=0)
    preflight_min_disk_gb: float = Field(default=10.0, ge=0)
    preflight_pressure_free_percent_max: int = Field(default=15, ge=0, le=100)
    preflight_pressure_consecutive_samples: int = Field(default=3, ge=1, le=20)
    preflight_pressure_sample_interval_seconds: float = Field(default=10.0, ge=0)

    def expanded(self, value: str) -> Path:
        return Path(value).expanduser().resolve()

    @property
    def allowed_roots(self) -> list[Path]:
        return [self.expanded(item.strip()) for item in self.allowed_path_roots.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
