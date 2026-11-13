from pathlib import Path

import pytest
from PIL import Image, ImageChops

from studio.character_art import character_frame, resolve_style
from studio.schemas import Character, Scene, VisualStyle


def character(**changes):
    fields = dict(name="Макс", age=18, face="Овальное", hairstyle="Короткая", hair_color="#392717", body="Среднее", clothing="Куртка", clothing_color="#517c61", voice="", personality="Ироничный", speech="Короткие фразы")
    return Character(**(fields | changes))


def draw(root: Path, name: str, passport):
    path = root / name
    character_frame(passport, Scene(character_id="max", text="Реплика"), path)
    with Image.open(path) as image:
        return image.copy()


@pytest.mark.parametrize("field,value", [
    ("hairstyle", "Длинные волосы"), ("hairstyle", "Кудрявые"), ("hairstyle", "Бритая голова"),
    ("body", "Худощавое"), ("body", "Крепкое"), ("clothing", "Худи"), ("clothing", "Футболка"),
    ("face", "Круглое"), ("face", "Угловатое"), ("accessories", ["Очки", "Серьги", "Цепочка"]),
])
def test_passport_features_change_pixels(tmp_path, field, value):
    base = draw(tmp_path, "base.png", character())
    variant = draw(tmp_path, "variant.png", character(**{field: value}))
    assert base.size == variant.size == (1080, 1920)
    difference = ImageChops.difference(base, variant)
    assert difference.getbbox() is not None
    # Внешность меняется на персонаже, а не только в фоне.
    assert difference.crop((200, 250, 900, 1400)).getbbox() is not None


def test_style_explicit_override_and_legacy_passport():
    passport = character(hairstyle="Длинные", clothing="Худи", accessories=["Очки"])
    inferred = resolve_style(passport)
    assert (inferred.haircut, inferred.outfit, inferred.glasses) == ("long", "hoodie", "round")
    passport.render_style = VisualStyle(haircut="shaved", outfit="tshirt", glasses="square")
    explicit = resolve_style(passport)
    assert (explicit.haircut, explicit.outfit, explicit.glasses) == ("shaved", "tshirt", "square")
    assert resolve_style(character(accessories=["Без очков"])).glasses == "none"


def test_character_image_is_deterministic(tmp_path):
    first = draw(tmp_path, "first.png", character())
    second = draw(tmp_path, "second.png", character())
    assert ImageChops.difference(first, second).getbbox() is None


def test_explicit_skin_and_accessory_styles(tmp_path):
    base = draw(tmp_path, "base.png", character())
    styled = draw(tmp_path, "styled.png", character(render_style=VisualStyle(skin_color="#84543d", glasses="square", earrings=True, necklace=True)))
    assert ImageChops.difference(base, styled).getbbox() is not None


def test_preview_api_owner_only_and_no_temporary_files(tmp_path):
    import io

    from fastapi.testclient import TestClient

    from studio.api import create_app
    from studio.config import Settings
    from studio.db import Store
    store = Store(tmp_path)
    store.put("character", "max", character().model_dump())
    settings = Settings(data=tmp_path, api_token="a"*40, worker_token="b"*40)
    client = TestClient(create_app(settings, store))
    assert client.get("/api/characters/max/preview").status_code == 401
    headers = {"Authorization": "Bearer "+"a"*40}
    assert client.get("/api/characters/missing/preview", headers=headers).status_code == 404
    assert client.get("/api/characters/max/preview?emotion=unknown", headers=headers).status_code == 422
    before = {p.name for p in tmp_path.iterdir()}
    response = client.get("/api/characters/max/preview?emotion=happy", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.size == (1080, 1920)
    assert {p.name for p in tmp_path.iterdir()} == before
