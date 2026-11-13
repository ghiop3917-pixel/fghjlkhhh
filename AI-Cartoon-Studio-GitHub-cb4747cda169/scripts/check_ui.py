"""Локальная проверка интерфейса. Не обращается к Telegram или AI-поставщикам."""
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
reports = root / "reports"
reports.mkdir(exist_ok=True)
test_data = reports / ("ui-data-" + secrets.token_hex(4))
token = secrets.token_hex(32)
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
env = {**os.environ, "STUDIO_API_TOKEN": token, "STUDIO_WORKER_TOKEN": secrets.token_hex(32), "STUDIO_SERVER_ENABLED": "true", "STUDIO_DATA": str(test_data), "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_OWNER_ID": ""}
process = subprocess.Popen([sys.executable, "-m", "studio", "api", "--port", str(port)], cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
try:
    for attempt in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            break
        except OSError:
            if process.poll() is not None:
                raise RuntimeError("Тестовый API не запустился")
            time.sleep(.1)
    else:
        raise RuntimeError("Истёк срок запуска тестового API")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(f"http://127.0.0.1:{port}") else route.abort())
        page.goto(f"http://127.0.0.1:{port}")
        page.locator("#token").fill(token)
        page.locator("#loginForm button").click()
        page.locator("#content").wait_for(state="visible")
        page.screenshot(path=str(reports/"panel-desktop.png"), full_page=True)
        for name in ("episodes", "characters", "tasks", "trends", "analytics", "agents", "settings"):
            page.locator(f'[data-view="{name}"]').click()
            assert page.locator("#content").inner_text().strip()
        page.locator("#readinessButton").click()
        page.locator("#readinessChecks").wait_for()
        assert page.locator("#readinessChecks tbody tr").count() >= 6
        page.locator("dialog .close").click()
        page.locator('[data-view="characters"]').click()
        page.locator('[data-action="newCharacter"]').click()
        page.locator("#objectId").fill("max")
        page.locator("#jsonForm button").click()
        page.locator("dialog").wait_for(state="hidden")
        page.get_by_role("heading", name="Макс", exact=True).wait_for()
        page.locator('[data-character-preview="max"]').click()
        page.locator("#characterPreview").wait_for()
        page.wait_for_function("document.querySelector('#characterPreview')?.naturalWidth === 1080")
        page.locator("#previewEmotion").select_option("angry")
        page.wait_for_function("document.querySelector('#characterPreview')?.dataset.emotion === 'angry'")
        page.locator("dialog .close").click()
        page.locator('[data-view="episodes"]').click()
        page.get_by_role("heading", name="Критические ситуации · сюжет + советы").wait_for()
        page.locator("[data-story-ideas]").click()
        page.get_by_text("ИИ придумает разные сюжетные ситуации без фиксированного списка тем.", exact=True).wait_for()
        page.locator("[data-emergency-auto]").click()
        page.get_by_text("ИИ придумает новые ситуации и предложит их на выбор.", exact=True).wait_for()
        page.locator('[data-emergency-topic="nuclear"]').click()
        page.get_by_text("Учебная история поставлена в очередь. Нужны персонаж и подключённая модель.", exact=True).wait_for()
        sample = json.loads((root/"examples"/"critical_situations"/"nuclear.json").read_text(encoding="utf-8"))
        imported = page.request.post(f"http://127.0.0.1:{port}/api/episodes/import", data=sample, headers={"Authorization": "Bearer "+token})
        assert imported.ok
        page.locator("#refresh").click()
        page.get_by_role("button", name="Проверить советы и источники").click()
        page.locator("#safetyForm").wait_for()
        for name in ("sources_checked", "advice_checked", "fictional_label_checked"):
            page.locator(f'#safetyForm input[name="{name}"]').check()
        page.locator("#safetyForm button").click()
        page.get_by_text("Проверка советов сохранена. Теперь можно согласовать подготовку.", exact=True).wait_for()
        # Только тестовая запись: файл не отправляется и реальный просмотр не имитируется.
        from studio.db import Store
        from studio.media import sha256
        sample.update(format="what_if_story", emergency_topic=None, safety_advice_ids=[])
        (test_data/"results").mkdir(exist_ok=True)
        fixture=test_data/"results"/"fixture-not-rendered.mp4"
        fixture.write_bytes(b"UI test fixture; not a real video")
        Store(test_data).put("episode", "delivery-ui-fixture", {"status": "reviewed", "script": sample, "result": fixture.name, "qc": {"sha256": sha256(fixture)}})
        page.locator("#refresh").click()
        page.get_by_role("button", name="Публикация и файлы", exact=True).click()
        page.locator("#deliveryPlan").click()
        page.locator('#planForm input[name="title"]').fill("Локальная проверка плана")
        page.locator('#planForm input[type="checkbox"]').check()
        page.locator("#planForm button").click()
        page.get_by_text("План сохранён; отключённый YouTube ожидает подключения.", exact=True).wait_for()
        page.get_by_role("button", name="Публикация и файлы", exact=True).click()
        page.get_by_role("button", name="Отменить план: Локальная проверка плана", exact=True).click()
        page.get_by_text("План отменён", exact=True).wait_for()
        page.locator("dialog .close").click()
        page.locator('[data-view="overview"]').click()
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(reports/"panel-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        browser.close()
    print("Панель: вход, 8 разделов, персонажи, эмоции, учебные истории, проверка советов, мобильная ширина — PASS")
finally:
    process.terminate()
    process.wait(timeout=10)
