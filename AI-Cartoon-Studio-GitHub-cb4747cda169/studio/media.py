"""На сервере создаются изображения, WAV, ASS и JSON. MP4 здесь не рендерится."""
import hashlib
import json
import math
import os
import shutil
import subprocess
import wave
from array import array
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from PIL import Image, ImageDraw

from studio.animation import speech_frames
from studio.character_art import RENDERER_VERSION, character_frame
from studio.emergencies import DISCLAIMER, briefing, validate_advice
from studio.schemas import Character, Script
from studio.speech import speech_command
from studio.subtitles import Captioner, groups


class SpeechDurationError(ValueError):
    """Сценарию нужна новая редакция с учётом реальной длительности речи."""


def sha256(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_file(root: Path, relative: str):
    if not relative or "\\" in relative or ":" in relative:
        raise ValueError("Некорректный путь материала")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError("Путь выходит за пределы пакета")
    return path


def legacy_character_frame(character: Character, scene, path: Path):
    # Постоянная геометрия и палитра обеспечивают идентичность базового 2D-рига.
    seed = int(hashlib.sha256(character.name.encode()).hexdigest()[:8], 16)
    image = Image.new("RGB", (1080, 1920), scene.background)
    d = ImageDraw.Draw(image)
    for i in range(7):
        x = i * 180 - 50
        height = 350 + ((seed >> (i*2)) % 450)
        d.rectangle((x, 1450-height, x+140, 1550), fill="#28374b")
        for y in range(1500-height, 1450, 90):
            d.rectangle((x+25, y, x+50, y+35), fill="#927642")
    d.ellipse((170, 1490, 920, 1620), fill="#111827")
    d.polygon([(380, 1230), (510, 1230), (500, 1540), (370, 1540)], fill="#273044")
    d.polygon([(550, 1230), (680, 1230), (710, 1540), (570, 1540)], fill="#273044")
    d.rounded_rectangle((300, 750, 760, 1280), radius=90, fill=character.clothing_color)
    d.rounded_rectangle((245, 830, 330, 1270), radius=40, fill=character.clothing_color)
    d.rounded_rectangle((735, 830, 820, 1270), radius=40, fill=character.clothing_color)
    d.rectangle((480, 650, 590, 810), fill="#d99d7c")
    d.ellipse((345, 320, 735, 780), fill="#e4af8c")
    d.pieslice((330, 270, 750, 650), 180, 355, fill=character.hair_color)
    d.ellipse((420, 500, 446, 530), fill="#18202d")
    d.ellipse((625, 500, 651, 530), fill="#18202d")
    if scene.emotion == "angry":
        d.line((402, 465, 465, 488), fill="#30221e", width=12)
        d.line((610, 488, 670, 465), fill="#30221e", width=12)
    if scene.emotion == "happy":
        d.arc((475, 590, 610, 690), 0, 180, fill="#692b35", width=12)
    else:
        d.line((485, 655, 600, 655), fill="#692b35", width=10)
    image.save(path)


def tts(text: str, output: Path, model: str):
    subprocess.run(speech_command(model, output), input=text, encoding="utf-8", check=True, capture_output=True, timeout=180)
    with wave.open(str(output), "rb") as audio:
        return audio.getnframes()/audio.getframerate()


def ass_time(seconds):
    cs = round(seconds * 100)
    return f"{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}"


def ambient_music(path: Path, duration: float):
    """Простой собственный синтезированный фон, без сторонних записей и API."""
    rate = 22050
    notes = [130.8128, 155.5635, 103.8262, 116.5409]
    samples = array("h")
    for i in range(round(duration * rate)):
        t = i / rate
        base = notes[int(t // 4) % len(notes)]
        envelope = min(1., t / 1.5, (duration-t) / 1.5)
        pulse = .7 + .3 * math.sin(t * math.pi)
        sample = (math.sin(2*math.pi*base*t) + .35*math.sin(2*math.pi*base*1.5*t)) * 2200 * max(0, envelope) * pulse
        samples.append(round(sample))
    import sys
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())


def input_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def voice_identity(model: str):
    """Изменение файла голоса по прежнему пути также сбрасывает озвучку."""
    try:
        engine_version = version("piper-tts")
    except PackageNotFoundError:
        engine_version = "not-installed"
    executable = shutil.which(os.getenv("PIPER_EXECUTABLE", "piper"))
    files = [Path(model), Path(model + ".json")] if model else []
    identity = {"model": model, "files": [sha256(p) if p.is_file() else None for p in files],
                "engine_version": engine_version, "executable": sha256(Path(executable)) if executable else None}
    if not model and os.getenv("STUDIO_TTS_FALLBACK", "none") == "espeak":
        fallback_exe = shutil.which(os.getenv("ESPEAK_EXECUTABLE", "espeak-ng"))
        identity.update(backend="espeak-ng", speed=os.getenv("STUDIO_TTS_WPM", "150"), executable=sha256(Path(fallback_exe)) if fallback_exe else None)
    return identity


class PreviousPackage:
    """Повторное использование только проверенных файлов, без изменения оригиналов."""
    def __init__(self, root: Path | None):
        self.root = root
        self.manifest = {}
        self.images, self.audio = {}, {}
        self.backgrounds, self.sprites = {}, {}
        if root is None:
            return
        try:
            self.manifest = validate_manifest(json.loads((root / "manifest.json").read_text(encoding="utf-8")))
            for scene in self.manifest["scenes"]:
                if scene.get("image_key"):
                    self.images[scene["image_key"]] = scene["image"]
                if scene.get("audio_key"):
                    self.audio[scene["audio_key"]] = scene["audio"]
                animation = scene.get("animation", {})
                if animation.get("background_key"):
                    self.backgrounds[animation["background_key"]] = animation["background"]
                if animation.get("sprite_key"):
                    self.sprites[animation["sprite_key"]] = animation["sprites"]
        except (OSError, ValueError, KeyError, TypeError):
            self.manifest = {}
            self.images, self.audio = {}, {}

    def copy(self, name: str | None, target: Path):
        if not name or self.root is None:
            return False
        try:
            info = self.manifest["files"][name]
            source = safe_file(self.root, name)
            if not source.is_file() or source.stat().st_size != info["size"] or sha256(source) != info["sha256"]:
                return False
            # Отдельная копия, а не hardlink: новая редакция не может изменить исходную.
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            # Ошибка записи новой копии должна остановить подготовку, а не перезаписать её.
            if target.exists():
                raise
            return False


def build_package(root: Path, script: Script, characters: dict[str, Character], synth=tts, reuse_root: Path | None = None, topics=None, backgrounds=None, exclude_audio=None):
    validate_advice(script, topics)
    captioner = Captioner()
    root.mkdir(parents=True, exist_ok=False)
    previous = PreviousPackage(reuse_root)
    reused = {"images": 0, "audio": 0, "music": 0}
    voices = {}
    backgrounds = backgrounds or {}
    exclude_audio = set(exclude_audio or [])
    # Никакие пути и команды от модели не попадают в manifest.
    manifest = {"version": 1, "width": 1080, "height": 1920, "fps": 30, "scenes": [], "files": {}}
    subtitles = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "",
                 "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
                 "Style: Default,DejaVu Sans,58,&H00FFFFFF,&H0000DFFF,&H00101010,&H80101010,-1,0,0,0,100,100,0,0,1,3,1,2,80,80,270,1", "", "[Events]",
                 "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    cursor = 0.0
    for index, scene in enumerate(script.scenes):
        character = characters.get(scene.character_id)
        if character is None:
            raise ValueError(f"Нет паспорта персонажа {scene.character_id}")
        image, audio = f"scene-{index:02}.png", f"scene-{index:02}.wav"
        voice = character.voice or os.getenv("PIPER_MODEL", "")
        if voice not in voices:
            voices[voice] = voice_identity(voice)
        appearance = character.model_dump(exclude={"voice", "personality", "speech"})
        cloud_background = backgrounds.get(index)
        cloud_hash = sha256(cloud_background) if cloud_background else None
        image_key = input_key({"renderer": RENDERER_VERSION, "appearance": appearance, "emotion": scene.emotion, "background": scene.background, "setting": scene.setting, "description": scene.description, "cloud_background": cloud_hash})
        audio_key = input_key({"engine": "piper-cli-v1", "text": scene.text, "voice": voices[voice]})
        if previous.copy(previous.images.get(image_key), root / image):
            reused["images"] += 1
        else:
            character_frame(character, scene, root / image)
        if index not in exclude_audio and previous.copy(previous.audio.get(audio_key), root / audio):
            with wave.open(str(root / audio), "rb") as wav:
                duration = wav.getnframes() / wav.getframerate()
            reused["audio"] += 1
        else:
            duration = synth(scene.text, root / audio, voice)
        if not 0.5 <= duration <= 25:
            raise SpeechDurationError("Длительность реплики вне 0,5–25 секунд")
        background_name = f"scene-{index:02}-background.png"
        background_key = input_key({"renderer": RENDERER_VERSION, "setting": scene.setting, "description": scene.description, "background": scene.background, "cloud_background": cloud_hash})
        if not previous.copy(previous.backgrounds.get(background_key), root/background_name):
            if cloud_background:
                shutil.copyfile(cloud_background, root/background_name)
            else:
                character_frame(character, scene, root/background_name, layer="background")
        sprite_key = input_key({"renderer": RENDERER_VERSION, "appearance": appearance, "emotion": scene.emotion})
        sprites = []
        old_sprites = previous.sprites.get(sprite_key, [])
        for variant in range(3):
            name = f"scene-{index:02}-pose-{variant}.png"
            source = old_sprites[variant] if len(old_sprites) == 3 else None
            if not previous.copy(source, root/name):
                character_frame(character, scene, root/name, layer="character", mouth_open=variant == 1, eyes_closed=variant == 2)
            sprites.append(name)
        animation = {"background": background_name, "background_key": background_key, "sprites": sprites, "sprite_key": sprite_key,
                     "fps": 8, "frames": speech_frames(root/audio, duration), "camera": scene.camera}
        if cloud_background:
            with Image.open(root/background_name) as bg, Image.open(root/sprites[0]) as foreground:
                Image.alpha_composite(bg.convert("RGBA"), foreground.convert("RGBA")).convert("RGB").save(root/image)
        manifest["scenes"].append({"image": image, "audio": audio, "duration": duration,
                                    "character_id": scene.character_id, "character_hash": hashlib.sha256(character.model_dump_json().encode()).hexdigest(),
                                    "image_key": image_key, "audio_key": audio_key, "animation": animation})
        alignment = captioner.align(root/audio, scene.text, duration)
        manifest["scenes"][-1]["alignment"] = {k: alignment[k] for k in ("method", "confidence")}
        # Короткие фразы; исходный текст не может внедрить ASS-команды.
        for group in groups(alignment["words"]):
            begin, end = cursor+group[0]["start"], cursor+group[-1]["end"]
            parts = []
            for word in group:
                clean = word["text"].replace("{", "").replace("}", "").replace("\\", "").replace("\n", " ")
                parts.append("{\\kf"+str(max(1, round((word["end"]-word["start"])*100)))+"}"+clean)
            line = " ".join(parts)
            subtitles.append(f"Dialogue: 0,{ass_time(begin)},{ass_time(end)},Default,,0,0,0,,{line}")
        cursor += duration
    if not 30 <= cursor <= 60:
        raise SpeechDurationError(f"Реальная озвучка {cursor:.1f} с; требуется 30–60 с. Исправьте длину сценария, сохранив смысл")
    manifest["duration"] = cursor
    if script.format == "emergency_story":
        subtitles.append(f"Dialogue: 0,{ass_time(0)},{ass_time(cursor)},Default,,90,90,70,,{{\\an8\\fs30}}Учебная история · события вымышлены\\NСледуйте указаниям местных служб")
    elif script.format == "what_if_story":
        subtitles.append(f"Dialogue: 0,{ass_time(0)},{ass_time(cursor)},Default,,90,90,70,,{{\\an8\\fs30}}А что, если… · вымышленная история")
    (root / "subtitles.ass").write_text("\n".join(subtitles), encoding="utf-8")
    manifest["subtitles"] = "subtitles.ass"
    manifest["music"] = None
    if script.music == "ambient":
        music_key = input_key({"generator": "ambient-v1", "duration": cursor})
        if previous.manifest.get("music_key") == music_key and previous.copy(previous.manifest.get("music"), root / "music.wav"):
            reused["music"] += 1
        else:
            ambient_music(root / "music.wav", cursor)
        manifest["music"] = "music.wav"
        manifest["music_key"] = music_key
    manifest["reuse"] = reused
    metadata = {"title": script.episode_title, "description": script.synopsis, "hashtags": ["#мультсериал", "#shorts"], "format": script.format}
    if script.format == "what_if_story":
        metadata["description"] += "\n\nВымышленная история «А что, если…»."
    if script.format == "emergency_story":
        info = briefing(script, topics)
        metadata.update(disclaimer=DISCLAIMER, safety_sources=info["advice"], script_sha256=info["script_sha256"])
        sources = dict.fromkeys(a["reference"]["url"] for a in info["advice"])
        metadata["description"] += "\n\n" + DISCLAIMER + "\nИсточники советов:\n" + "\n".join(sources)
    (root / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    with Image.open(root / "scene-00.png") as cover:
        cover.save(root / "cover.png")
    for path in root.iterdir():
        if path.is_file():
            manifest["files"][path.name] = {"sha256": sha256(path), "size": path.stat().st_size}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest


def validate_manifest(manifest: dict):
    if manifest.get("version") != 1 or (manifest.get("width"), manifest.get("height"), manifest.get("fps")) != (1080, 1920, 30):
        raise ValueError("Неподдерживаемый формат монтажного пакета")
    scenes = manifest.get("scenes", [])
    if not 1 <= len(scenes) <= 12:
        raise ValueError("Неверное число сцен")
    duration = sum(float(s["duration"]) for s in scenes)
    if not 30 <= duration <= 60 or abs(duration-float(manifest["duration"])) > .01:
        raise ValueError("Неверный тайминг")
    files = manifest.get("files", {})
    if len(files) > 100 or sum(f["size"] for f in files.values()) > 500_000_000:
        raise ValueError("Пакет слишком велик")
    for name, info in files.items():
        safe_file(Path.cwd(), name)
        if len(info["sha256"]) != 64 or not 0 < info["size"] <= 100_000_000:
            raise ValueError("Неверное описание файла")
    for scene in scenes:
        if not .5 <= float(scene["duration"]) <= 25 or scene["image"] not in files or scene["audio"] not in files:
            raise ValueError("Не хватает материалов сцены")
        if scene.get("animation"):
            animation = scene["animation"]
            if animation.get("fps") != 8 or len(animation.get("sprites", [])) != 3 or animation.get("background") not in files:
                raise ValueError("Некорректные слои анимации")
            if any(name not in files for name in animation["sprites"]) or any(frame not in (0,1,2) for frame in animation.get("frames", [])):
                raise ValueError("Неизвестный кадр анимации")
            if len(animation.get("frames", [])) != math.ceil(float(scene["duration"])*8) or animation.get("camera") not in ("push_in", "pull_out", "pan_left", "pan_right", "static"):
                raise ValueError("Некорректный таймлайн анимации")
    if manifest.get("subtitles") not in files:
        raise ValueError("Не хватает субтитров")
    if manifest.get("music") is not None and manifest["music"] not in files:
        raise ValueError("Не хватает музыкальной дорожки")
    return manifest


def technical_qc(path: Path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, timeout=30, check=True)
    probe = json.loads(result.stdout)
    video = next((s for s in probe["streams"] if s["codec_type"] == "video"), {})
    audio = next((s for s in probe["streams"] if s["codec_type"] == "audio"), {})
    duration = float(probe["format"].get("duration", 0))
    issues = []
    if (video.get("width"), video.get("height")) != (1080, 1920):
        issues.append("Ожидается 1080×1920")
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        issues.append("Ожидается H.264 yuv420p")
    if not audio or audio.get("codec_name") != "aac":
        issues.append("Отсутствует AAC-аудио")
    if not 30 <= duration <= 60.2:
        issues.append("Длительность вне 30–60 секунд")
    if audio and abs(float(audio.get("duration", duration))-duration) > .3:
        issues.append("Длительности видео и звука расходятся")
    decode = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"], capture_output=True, timeout=180)
    if decode.returncode:
        issues.append("Ошибка полного декодирования")
    return {"pass": not issues, "issues": issues, "duration": duration, "sha256": sha256(path), "scope": "technical_only"}
