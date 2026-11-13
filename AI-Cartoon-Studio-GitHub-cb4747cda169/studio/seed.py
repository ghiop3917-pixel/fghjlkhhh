"""Начальный персонаж; существующие паспорта не перезаписываются."""
from studio.schemas import Character


def seed(store):
    if store.get("character", "max") is not None:
        return {"created": False, "character": "max"}
    passport = Character(name="Макс", age=20, face="Овальное лицо", hairstyle="Короткая", hair_color="#392717", body="Среднее", clothing="Куртка", clothing_color="#517c61", voice="", personality="Наблюдательный, ироничный, ищет выход из трудных ситуаций", speech="Короткие живые фразы")
    store.put("character", "max", passport.model_dump(), expected=0)
    return {"created": True, "character": "max"}
