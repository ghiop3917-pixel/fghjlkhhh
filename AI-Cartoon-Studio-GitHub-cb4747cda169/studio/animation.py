"""Серверные декорации и таймлайн артикуляции; финальный MP4 здесь не создаётся."""
import math
import sys
import wave
from array import array

from PIL import Image, ImageDraw


def background_image(scene, seed=0):
    image = Image.new("RGB", (1080, 1920), scene.background)
    d = ImageDraw.Draw(image)
    setting = scene.setting
    if setting in ("interior", "shelter", "station"):
        d.rectangle((0, 0, 1080, 1470), fill="#5d6269" if setting == "shelter" else "#3d5265")
        d.polygon([(0, 1470), (1080, 1470), (1080, 1920), (0, 1920)], fill="#25313f")
        for x in range(-500, 1500, 240):
            d.line((540, 1470, x, 1920), fill="#405164", width=4)
        if setting == "shelter":
            d.rounded_rectangle((750, 250, 1040, 1370), radius=15, fill="#37444b", outline="#869b9e", width=12)
            d.ellipse((950, 820, 985, 855), fill="#afc5bb")
            for y in (200, 330):
                d.line((0, y, 1080, y), fill="#818a8b", width=32)
        elif setting == "interior":
            d.rectangle((55, 240, 375, 720), fill="#83a8b9", outline="#a7b5b8", width=20)
            d.line((215, 240, 215, 720), fill="#bdc9ca", width=12)
            d.line((55, 480, 375, 480), fill="#bdc9ca", width=12)
            d.rectangle((780, 1000, 1080, 1400), fill="#7c6252")
        else:
            for x in (40, 790):
                d.rectangle((x, 200, x+125, 1470), fill="#758292")
            d.rounded_rectangle((300, 170, 730, 300), radius=20, fill="#244b43")
    elif setting in ("island", "desert"):
        d.rectangle((0, 0, 1080, 900), fill="#7bc3d9")
        d.ellipse((790, 180, 950, 340), fill="#f7df9c")
        d.rectangle((0, 900, 1080, 1920), fill="#379ba9" if setting == "island" else "#cfaa6e")
        d.ellipse((-220, 1230, 1320, 2450), fill="#e5c18b")
        if setting == "island":
            for y in (960, 1050, 1160):
                d.line((0, y, 1080, y), fill="#8bd6d1", width=8)
            d.line((140, 1430, 230, 530), fill="#805a3a", width=40)
            for x,y in ((-80, 430), (50, 290), (460, 350), (570, 650), (0, 760)):
                d.polygon([(230, 530), (x, y), ((230+x)//2, (530+y)//2+90)], fill="#246957")
        else:
            d.polygon([(0, 1190), (420, 870), (760, 1270)], fill="#dab680")
    elif setting == "forest":
        d.rectangle((0, 0, 1080, 1920), fill="#294d46")
        for i in range(9):
            x = i*150-80
            d.rectangle((x, 250, x+65, 1750), fill="#4d4636")
            d.ellipse((x-170, -100+(i%3)*160, x+240, 850+(i%3)*100), fill="#326657")
        d.rectangle((0, 1540, 1080, 1920), fill="#3c5140")
        d.polygon([(480, 1400), (640, 1400), (950, 1920), (120, 1920)], fill="#76684e")
    elif setting == "space":
        d.rectangle((0, 0, 1080, 1920), fill="#0d162e")
        for i in range(80):
            x, y = ((seed+i*157)%1080, (seed//7+i*293)%1450)
            d.ellipse((x, y, x+4, y+4), fill="#d8e4ed")
        d.ellipse((650, 180, 1120, 650), fill="#5d71a4")
        d.polygon([(0, 1490), (1080, 1490), (1080, 1920), (0, 1920)], fill="#39445b")
    else:
        for i in range(7):
            x = i*180-50
            height = 350+((seed >> (i*2))%450)
            d.rectangle((x, 1450-height, x+140, 1600), fill="#28374b")
            for y in range(1500-height, 1450, 90):
                d.rectangle((x+25, y, x+50, y+35), fill="#927642")
        d.rectangle((0, 1600, 1080, 1920), fill="#121c29")
        if setting == "rooftop":
            d.rectangle((0, 1440, 1080, 1530), fill="#79808b")
            for x in range(0, 1080, 160):
                d.line((x, 1440, x, 1690), fill="#79808b", width=12)
    return image


def speech_frames(path, duration, fps=8):
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("Анализ артикуляции ожидает PCM16 WAV")
        channels, rate = wav.getnchannels(), wav.getframerate()
        samples = array("h", wav.readframes(wav.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
    energies = []
    count = math.ceil(duration*fps)
    for frame in range(count):
        left, right = int(frame*rate/fps)*channels, int((frame+1)*rate/fps)*channels
        chunk = samples[left:right]
        energies.append(math.sqrt(sum(v*v for v in chunk)/max(1,len(chunk))))
    threshold = max(120, max(energies, default=0)*.12)
    return [2 if i > 0 and i%27 == 0 else 1 if energy > threshold and i%3 != 0 else 0 for i,energy in enumerate(energies)]
