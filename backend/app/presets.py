from pydantic import BaseModel, Field, model_validator


SCORPION_FROG_MOONLIT_RIVERBANK_PROMPT = (
    "On a quiet moonlit riverbank, the frog and scorpion pause together at the water’s edge; "
    "silver-blue moonlight ripples across the river, reeds and ferns sway in a gentle night breeze, "
    "a slow cinematic camera push-in, soft blue mist, natural storybook realism, calm and contemplative mood."
)


class Preset(BaseModel):
    name: str
    frames: int = Field(ge=9, le=193)
    fps: int = Field(ge=1, le=60)
    status: str = "Custom"
    visual_drift_warning: bool = False

    @model_validator(mode="after")
    def validate_frame_stride(self):
        if (self.frames - 1) % 8:
            raise ValueError("Frame count must be 8n + 1 for LTX")
        return self

    @property
    def duration(self) -> float:
        return round(self.frames / self.fps, 2)


BUILTIN_PRESETS = [
    Preset(name="Short", frames=49, fps=24, status="Verified"),
    Preset(name="Standard", frames=97, fps=24, status="Default"),
    Preset(name="Extended", frames=145, fps=24, status="Verified"),
    Preset(name="Experimental", frames=193, fps=24, status="Visual drift warning", visual_drift_warning=True),
]


class GenerationSettings(BaseModel):
    model: str = "dgrauet/ltx-2.3-mlx-q8"
    width: int = Field(default=448, ge=64, le=2048)
    height: int = Field(default=768, ge=64, le=2048)
    distilled_two_stage: bool = True
    low_ram: bool = True
    audio: bool = False
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    prompt: str = Field(default="", max_length=4000)
    reference_image: str | None = None
    output_location: str | None = None
    preset: str = "Standard"

    @model_validator(mode="after")
    def validate_resolution(self):
        if self.width % 8 or self.height % 8:
            raise ValueError("Width and height must be divisible by 8")
        return self
