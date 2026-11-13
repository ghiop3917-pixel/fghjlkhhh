"""Детерминированный фронтальный 2D-персонаж: без GPU и внешних запросов."""
import hashlib
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageDraw

from studio.animation import background_image
from studio.schemas import Character, Scene, VisualStyle

RENDERER_VERSION = "2d-v3"
INK = "#15202b"


def resolve_style(character: Character) -> VisualStyle:
    if character.render_style is not None:
        return character.render_style
    hair, body, clothes, face = (str(s).casefold() for s in
                                  (character.hairstyle, character.body, character.clothing, character.face))
    accessories = " ".join(character.accessories).casefold()
    haircut = "short"
    if any(s in hair for s in ("брит", "лыс", "без волос")):
        haircut = "shaved"
    elif any(s in hair for s in ("кудр", "вью", "curly")):
        haircut = "curly"
    elif any(s in hair for s in ("длин", "long")):
        haircut = "long"
    return VisualStyle(
        haircut=haircut,
        build="slim" if any(s in body for s in ("худ", "строй", "slim")) else "broad" if any(s in body for s in ("широк", "креп", "плот", "атлет", "broad")) else "regular",
        outfit="hoodie" if any(s in clothes for s in ("худи", "капюшон", "hoodie")) else "tshirt" if any(s in clothes for s in ("футбол", "tshirt", "t-shirt")) else "jacket",
        face_shape="round" if any(s in face for s in ("круг", "round")) else "angular" if any(s in face for s in ("углов", "квадрат", "angular")) else "oval",
        glasses="round" if "очк" in accessories and "без очков" not in accessories else "none",
        earrings=any(s in accessories for s in ("серьг", "серёж", "сереж")),
        necklace=any(s in accessories for s in ("цепоч", "кулон", "ожерел")),
    )


def character_frame(character: Character, scene: Scene, path: Path | BinaryIO, *, layer="full", mouth_open=False, eyes_closed=False):
    style = resolve_style(character)
    seed = int(hashlib.sha256((scene.setting+scene.description).encode()).hexdigest()[:8], 16)
    image = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0)) if layer == "character" else background_image(scene, seed)
    if layer == "background":
        image.save(path, format="PNG")
        return
    d = ImageDraw.Draw(image)
    # Место действия постоянно между персонажами; мимика меняет только передний план.
    d.ellipse((170, 1510, 920, 1640), fill="#0b1420")
    half = {"slim": 165, "regular": 215, "broad": 255}[style.build]
    left, right = 540-half, 540+half
    skin, cloth, hair = style.skin_color, character.clothing_color, character.hair_color
    for x in (430, 575):
        d.rounded_rectangle((x, 1190, x+115, 1550), radius=25, fill="#303e54", outline=INK, width=6)
        d.rounded_rectangle((x-22, 1510, x+130, 1580), radius=22, fill="#0e1723", outline=INK, width=5)
    if style.haircut == "long":
        d.rounded_rectangle((320, 300, 760, 960), radius=170, fill=hair, outline=INK, width=6)
    if style.outfit == "hoodie":
        d.ellipse((left+10, 655, right-10, 990), fill=cloth, outline=INK, width=7)
        d.ellipse((left+50, 690, right-50, 920), fill="#25303e")
    d.rounded_rectangle((left, 780, right, 1280), radius=70, fill=cloth, outline=INK, width=7)
    for a, b in ((left-75, left+25), (right-25, right+75)):
        if style.outfit == "tshirt":
            d.rounded_rectangle((a+12, 895, b-12, 1280), radius=35, fill=skin, outline=INK, width=5)
            d.rounded_rectangle((a, 810, b, 1000), radius=35, fill=cloth, outline=INK, width=6)
        else:
            d.rounded_rectangle((a, 810, b, 1250), radius=40, fill=cloth, outline=INK, width=6)
        d.ellipse((a+12, 1220, b-12, 1300), fill=skin, outline=INK, width=5)
    d.rectangle((485, 665, 595, 820), fill=skin, outline=INK, width=5)
    if style.outfit == "jacket":
        d.line((540, 810, 540, 1270), fill=INK, width=8)
        d.polygon([(left+40, 795), (485, 795), (525, 930)], fill="#263344", outline=INK, width=5)
        d.polygon([(right-40, 795), (595, 795), (555, 930)], fill="#263344", outline=INK, width=5)
        d.line((left+45, 1110, left+125, 1065), fill=INK, width=7)
        d.line((right-45, 1110, right-125, 1065), fill=INK, width=7)
    elif style.outfit == "hoodie":
        d.arc((430, 745, 650, 895), 0, 180, fill=INK, width=7)
        d.line((490, 840, 485, 1010), fill="#e4e0d4", width=7)
        d.line((590, 840, 595, 1010), fill="#e4e0d4", width=7)
        d.rounded_rectangle((left+45, 1090, right-45, 1205), radius=30, outline=INK, width=6)
    else:
        d.arc((460, 750, 620, 875), 0, 180, fill=INK, width=10)
    if style.necklace:
        d.arc((445, 755, 635, 1020), 0, 180, fill="#e9c477", width=8)
        d.ellipse((528, 1000, 552, 1030), fill="#e9c477", outline=INK, width=3)
    # Уши находятся за контуром лица; украшения рисуются поверх.
    for x in (338, 692):
        d.ellipse((x, 500, x+50, 610), fill=skin, outline=INK, width=5)
    if style.face_shape == "angular":
        d.polygon([(365, 350), (715, 350), (725, 580), (665, 720), (540, 765), (415, 720), (355, 580)], fill=skin, outline=INK, width=6)
    elif style.face_shape == "round":
        d.ellipse((335, 340, 745, 750), fill=skin, outline=INK, width=6)
    else:
        d.ellipse((350, 315, 730, 775), fill=skin, outline=INK, width=6)
    if style.haircut == "short":
        d.pieslice((340, 285, 740, 610), 180, 355, fill=hair, outline=INK, width=6)
        d.polygon([(350, 450), (415, 330), (650, 325), (720, 450), (650, 405), (575, 430), (490, 395), (410, 445)], fill=hair)
    elif style.haircut == "long":
        d.pieslice((325, 270, 755, 640), 180, 350, fill=hair, outline=INK, width=6)
        d.polygon([(355, 425), (430, 320), (665, 340), (715, 460), (575, 390)], fill=hair)
    elif style.haircut == "curly":
        for x, y in ((365, 365), (405, 315), (465, 290), (535, 280), (605, 295), (670, 325), (710, 380)):
            d.ellipse((x-55, y-50, x+55, y+60), fill=hair, outline=INK, width=5)
    else:
        d.arc((365, 330, 715, 665), 190, 350, fill=hair, width=12)
    for x in (435, 635):
        if eyes_closed:
            d.line((x-17, 525, x+17, 525), fill=INK, width=7)
        else:
            d.ellipse((x-13, 510, x+13, 540), fill=INK)
    if scene.emotion == "angry":
        d.line((400, 470, 470, 495), fill=hair, width=12)
        d.line((600, 495, 670, 470), fill=hair, width=12)
    else:
        d.arc((400, 475, 470, 510), 195, 345, fill=hair, width=8)
        d.arc((600, 475, 670, 510), 195, 345, fill=hair, width=8)
    d.line((535, 535, 520, 600, 550, 600), fill="#b87e63", width=6)
    if mouth_open:
        d.ellipse((510, 635, 580, 685), fill="#512a35", outline=INK, width=4)
    elif scene.emotion == "happy":
        d.arc((475, 600, 610, 690), 0, 180, fill="#692b35", width=12)
    elif scene.emotion == "angry":
        d.arc((485, 650, 600, 700), 190, 350, fill="#692b35", width=10)
    else:
        d.line((490, 655, 595, 655), fill="#692b35", width=9)
    if style.glasses != "none":
        for x in (435, 635):
            bounds = (x-58, 480, x+58, 570)
            if style.glasses == "round":
                d.ellipse(bounds, outline=INK, width=9)
            else:
                d.rounded_rectangle(bounds, radius=15, outline=INK, width=9)
        d.line((493, 515, 577, 515), fill=INK, width=8)
        d.line((360, 510, 377, 520), fill=INK, width=8)
        d.line((693, 520, 720, 510), fill=INK, width=8)
    if style.earrings:
        for x in (355, 720):
            d.ellipse((x-13, 585, x+13, 630), outline="#e9c477", width=7)
    image.save(path, format="PNG")
