"""Windows Worker: только загрузка пакета, сборка FFmpeg и возврат MP4."""
import asyncio
import os
import platform
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from studio.config import flag
from studio.media import safe_file, sha256, validate_manifest


def render(folder: Path, manifest: dict, encoder="auto", progress=None):
    validate_manifest(manifest)
    for name, info in manifest["files"].items():
        path = safe_file(folder, name)
        if not path.is_file() or path.stat().st_size != info["size"] or sha256(path) != info["sha256"]:
            raise ValueError("Нарушена целостность материала")
    selected = encoder
    if encoder == "auto":
        test = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=64x64", "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True, timeout=30)
        selected = "h264_nvenc" if test.returncode == 0 else "libx264"
    if selected not in ("libx264", "h264_nvenc"):
        raise ValueError("Неподдерживаемый кодировщик")
    # Отдельная папка каждой попытки исключает перезапись исходных материалов.
    work = folder / ("render-" + uuid.uuid4().hex)
    work.mkdir()
    for index, scene in enumerate(manifest["scenes"]):
        if progress:
            progress('rendering', index+1, len(manifest['scenes']))
        frames = round(float(scene["duration"])*30)
        if scene.get("animation"):
            animation = scene["animation"]
            for variant,name in enumerate(animation["sprites"]):
                shutil.copyfile(safe_file(folder, name), work/f"actor-{index:02}-{variant}.png")
            sequence = []
            for variant in animation["frames"]:
                sequence.extend([f"file 'actor-{index:02}-{variant}.png'", "duration 0.125"])
            sequence.append(f"file 'actor-{index:02}-{animation['frames'][-1]}.png'")
            timeline = work/f"animation-{index:02}.txt"
            timeline.write_text("\n".join(sequence), encoding="utf-8")
            camera = animation["camera"]
            zoom = f"1+0.025*on/{frames}" if camera == "push_in" else f"max(1,1.025-0.025*on/{frames})" if camera == "pull_out" else "1"
            pan = f"40*t/{scene['duration']}" if camera == "pan_right" else f"40-40*t/{scene['duration']}" if camera == "pan_left" else "20+10*sin(t/4)" if camera != "static" else "20"
            graph = (f"[0:v]scale=1120:1992,crop=1080:1920:x='{pan}':y=36[bg];"
                     "[1:v]format=rgba,fps=30[fg];[bg][fg]overlay=x='5*sin(t*1.3)':y='3*sin(t*2)':eof_action=repeat[body];"
                     f"[body]zoompan=z='{zoom}':d=1:s=1080x1920:fps=30,format=yuv420p[v]")
            # Относительные имена обходят ограничения длинных путей ряда Windows-сборок FFmpeg.
            background_input = os.path.relpath(safe_file(folder, animation["background"]), work)
            audio_input = os.path.relpath(safe_file(folder, scene["audio"]), work)
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-filter_complex_threads", "1", "-loop", "1", "-i", background_input,
                            "-f", "concat", "-safe", "1", "-i", timeline.name, "-i", audio_input, "-filter_complex", graph,
                            "-map", "[v]", "-map", "2:a:0", "-t", str(scene["duration"]), "-r", "30", "-c:v", selected, "-threads", "2", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-af", "apad", f"part-{index:02}.mp4"], cwd=work, check=True, capture_output=True, timeout=1200)
            continue
        vf = f"scale=1120:1992,zoompan=z='min(zoom+0.00012,1.035)':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d={frames}:s=1080x1920:fps=30,format=yuv420p"
        command = ["ffmpeg", "-nostdin", "-v", "error", "-loop", "1", "-i", str(safe_file(folder, scene["image"])), "-i", str(safe_file(folder, scene["audio"])),
                   "-vf", vf, "-t", str(scene["duration"]), "-r", "30", "-c:v", selected, "-threads", "2", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-af", "apad", "-pix_fmt", "yuv420p", str(work/f"part-{index:02}.mp4")]
        subprocess.run(command, check=True, capture_output=True, timeout=900)
    (work/"concat.txt").write_text("\n".join(f"file 'part-{i:02}.mp4'" for i in range(len(manifest["scenes"]))), encoding="utf-8")
    # Фиксированное локальное имя в фильтре, без интерполяции путей из manifest.
    (work/"subtitles.ass").write_bytes(safe_file(folder, manifest["subtitles"]).read_bytes())
    output = work/"final.mp4"
    if progress:
        progress('assembling', len(manifest['scenes']), len(manifest['scenes']))
    command = ["ffmpeg", "-nostdin", "-v", "error", "-f", "concat", "-safe", "1", "-i", "concat.txt"]
    if manifest.get("music"):
        command += ["-i", str(safe_file(folder, manifest["music"])), "-filter_complex",
                    "[0:v]ass=subtitles.ass[v];[0:a]loudnorm=I=-16:TP=-1.5:LRA=11[voice];[1:a]volume=0.18[music];[voice][music]amix=inputs=2:duration=first:normalize=0[a]",
                    "-map", "[v]", "-map", "[a]"]
    else:
        command += ["-vf", "ass=subtitles.ass", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    command += ["-c:v", selected, "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "final.mp4"]
    subprocess.run(command, cwd=work, check=True, capture_output=True, timeout=1800)
    return output


def server_url():
    url = os.getenv("STUDIO_API_URL", "http://127.0.0.1:8000").rstrip("/")
    parsed = urlparse(url)
    loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.username or parsed.password or (parsed.scheme != "https" and not (loopback and parsed.scheme == "http" and flag("STUDIO_ALLOW_HTTP_LOOPBACK", True))):
        raise ValueError("Worker требует HTTPS; HTTP разрешён только для локального теста")
    return url


async def run_worker(once=False):
    if platform.system() != "Windows":
        raise RuntimeError("Финальный рендер разрешён только Windows Worker")
    token = os.getenv("STUDIO_WORKER_TOKEN", "")
    if len(token) < 32:
        raise ValueError("Не задан токен Worker")
    root = Path(os.getenv("STUDIO_WORKER_DATA", "worker-data")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(base_url=server_url(), headers={"Authorization": "Bearer " + token}, timeout=120, follow_redirects=False) as client:
        while True:
            try:
                from studio.comfort import worker_status
                response = await client.post("/worker/claim", json={"worker_id": os.getenv("STUDIO_WORKER_ID", "lenovo-render-1"), **worker_status(root)})
                response.raise_for_status()
                task = response.json()
                if task:
                    await perform(client, root, task)
            except (httpx.HTTPError, OSError, ValueError):
                print("Нет соединения с сервером; очередь сохранена на сервере")
            if once:
                return
            await asyncio.sleep(10)


async def perform(client, root, task):
    id, lease = task["id"], task["lease_token"]
    folder = root / (id + "-" + lease)
    folder.mkdir(exist_ok=True)
    stopped = asyncio.Event()
    lease_lost = asyncio.Event()
    progress = {"stage": "downloading", "scene": 0, "scenes": 0}
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def update(stage, scene=0, scenes=0):
        def apply():
            progress.update(stage=stage, scene=scene, scenes=scenes)
            changed.set()
        loop.call_soon_threadsafe(apply)

    async def heartbeat():
        while not stopped.is_set():
            try:
                changed.clear()
                result = await client.post(f"/worker/{id}/heartbeat", json={"lease_token": lease, **progress})
                result.raise_for_status()
            except httpx.HTTPError:
                lease_lost.set()
                return
            try:
                await asyncio.wait_for(changed.wait(), 25)
            except TimeoutError:
                pass

    pulse = asyncio.create_task(heartbeat())
    try:
        response = await client.get(f"/worker/{id}/manifest", params={"lease": lease})
        response.raise_for_status()
        manifest = validate_manifest(response.json())
        for name, info in manifest["files"].items():
            path = safe_file(folder, name)
            if path.is_file() and sha256(path) == info["sha256"]:
                continue
            if path.exists():
                raise ValueError("Обнаружен повреждённый локальный файл; сохранён для диагностики")
            size = 0
            async with client.stream("GET", f"/worker/{id}/asset/{name}", params={"lease": lease}) as r:
                r.raise_for_status()
                with path.open("xb") as out:
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > info["size"]:
                            raise ValueError("Превышен ожидаемый размер материала")
                        out.write(chunk)
            if sha256(path) != info["sha256"]:
                raise ValueError("Контрольная сумма не совпала")
        result = await asyncio.to_thread(render, folder, manifest, os.getenv("STUDIO_ENCODER", "auto"), update)
        if lease_lost.is_set():
            raise ValueError("Аренда потеряна: MP4 оставлен локально, устаревший результат не отправляется")
        update('uploading')
        async def chunks():
            with result.open("rb") as source:
                while chunk := source.read(1024*1024):
                    yield chunk
        r = await client.put(f"/worker/{id}/result", params={"lease": lease}, content=chunks(), headers={"Content-Type": "video/mp4"})
        r.raise_for_status()
        print("Рендер отправлен на сервер, ожидает проверки качества")
    except (httpx.HTTPError, ValueError, OSError, subprocess.SubprocessError):
        if not lease_lost.is_set():
            try:
                await client.post(f"/worker/{id}/failed", json={"lease_token": lease})
            except httpx.HTTPError:
                pass
        print("Рендер не завершён; материалы сохранены, сервер восстановит задачу")
    finally:
        stopped.set()
        changed.set()
        await pulse
