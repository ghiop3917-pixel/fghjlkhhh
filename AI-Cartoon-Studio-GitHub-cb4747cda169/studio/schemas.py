from typing import Literal

from pydantic import BaseModel, Field, model_validator


class VisualStyle(BaseModel):
    haircut: Literal["short", "long", "shaved", "curly"] = "short"
    build: Literal["slim", "regular", "broad"] = "regular"
    outfit: Literal["jacket", "hoodie", "tshirt"] = "jacket"
    face_shape: Literal["oval", "round", "angular"] = "oval"
    skin_color: str = Field(default="#e4af8c", pattern=r"^#[0-9a-fA-F]{6}$")
    glasses: Literal["none", "round", "square"] = "none"
    earrings: bool = False
    necklace: bool = False


class Character(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    age: int = Field(ge=16, le=100)
    face: str
    hairstyle: str
    hair_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    body: str
    clothing: str
    clothing_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    accessories: list[str] = []
    voice: str
    personality: str
    speech: str
    references: list[str] = []
    poses: list[str] = ["standing"]
    emotions: list[str] = ["neutral", "angry", "happy"]
    angles: list[str] = ["front"]
    render_style: VisualStyle | None = None


class Scene(BaseModel):
    character_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    text: str = Field(min_length=1, max_length=600)
    emotion: Literal["neutral", "angry", "happy"] = "neutral"
    background: str = Field(default="#182338", pattern=r"^#[0-9a-fA-F]{6}$")
    setting: Literal["street", "island", "forest", "interior", "shelter", "station", "desert", "space", "rooftop"] = "street"
    camera: Literal["push_in", "pull_out", "pan_left", "pan_right", "static"] = "push_in"
    description: str = Field(default="", max_length=600)


class Script(BaseModel):
    season_title: str = Field(min_length=1, max_length=100)
    episode_title: str = Field(min_length=1, max_length=100)
    synopsis: str = Field(min_length=1, max_length=1500)
    scenes: list[Scene] = Field(min_length=3, max_length=12)
    next_plan: str = Field(min_length=1, max_length=1000)
    unresolved: list[str] = Field(max_length=15)
    music: Literal["ambient", "none"] = "ambient"
    format: Literal["drama", "emergency_story", "what_if_story"] = "drama"
    emergency_topic: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    safety_advice_ids: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def length(self):
        if self.format == "emergency_story" and (not self.emergency_topic or not self.safety_advice_ids):
            raise ValueError("Для учебной истории нужны emergency_topic и safety_advice_ids")
        if self.format != "emergency_story" and (self.emergency_topic or self.safety_advice_ids):
            raise ValueError("Советы по ЧС требуют формата emergency_story")
        words = sum(len(s.text.split()) for s in self.scenes)
        if not 55 <= words <= 145:
            raise ValueError("Нужно 55–145 слов; длительность затем проверяется по реальной озвучке")
        return self


class Edit(BaseModel):
    category: Literal["character", "story", "preferences", "rules", "series"]
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    body: dict
    expected_revision: int = Field(ge=0)


class Lease(BaseModel):
    lease_token: str
    stage: Literal['downloading', 'rendering', 'assembling', 'uploading'] | None = None
    scene: int = Field(default=0, ge=0, le=12)
    scenes: int = Field(default=0, ge=0, le=12)


class WorkerClaim(BaseModel):
    worker_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    free_bytes: int | None = Field(default=None, ge=0)
    ffmpeg_ready: bool | None = None

