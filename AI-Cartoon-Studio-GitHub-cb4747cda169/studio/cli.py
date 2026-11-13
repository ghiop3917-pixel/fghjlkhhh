import argparse
import asyncio
import json

from studio.config import load
from studio.db import Store


def main():
    parser = argparse.ArgumentParser(description="AI Cartoon Studio: сервер отдельно, рендер отдельно")
    parser.add_argument("command", choices=["doctor", "api", "production", "bot", "worker", "backup", "seed"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--profile", choices=["worker", "server", "deployment"], help="Профиль проверки doctor")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Диагностика в JSON без секретов")
    parser.add_argument("--strict", action="store_true", help="Код выхода 2 при недостающих настройках")
    args = parser.parse_args()
    settings = load()
    if args.command == "doctor":
        from studio.readiness import readiness, text_report
        report = readiness(settings, args.profile or ("server" if settings.server_enabled else "worker"))
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json_output else text_report(report))
        if args.strict and not report["configuration_ready"]:
            raise SystemExit(2)
        return
    if args.command == "worker":
        from studio.worker import run_worker
        asyncio.run(run_worker(args.once))
        return
    if not settings.server_enabled:
        parser.error("Серверные процессы отключены. На Lenovo работает только worker. Для явного локального теста задайте STUDIO_SERVER_ENABLED=true в отдельной тестовой среде.")
    store = Store(settings.data)
    if args.command == "api":
        import uvicorn

        from studio.api import create_app
        uvicorn.run(create_app(settings, store), host=args.host, port=args.port, access_log=False)
    elif args.command == "production":
        from studio.production import Production
        service = Production(store)
        asyncio.run(service.tick() if args.once else service.run())
    elif args.command == "bot":
        from studio.bot import Bot
        asyncio.run(Bot(store, settings).run())
    elif args.command == "backup":
        print("Резервная копия:", store.backup())
    elif args.command == "seed":
        from studio.seed import seed
        result=seed(store)
        from studio.config import flag
        if flag("STUDIO_STORY_AUTOPLAN", True) and not store.objects("episode") and not store.objects("trend"):
            from studio.story_ideas import request_story_ideas
            result["initial_ideas_task"]=request_story_ideas(store, "first-start-story-ideas")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
