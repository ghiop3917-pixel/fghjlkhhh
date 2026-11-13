@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo AI CARTOON STUDIO
echo 1. Проверить окружение
echo 2. Создать .env без перезаписи
echo 3. Запустить Windows Render Worker
echo 4. Запустить целевые тесты
echo 5. Проверить готовность переноса на VPS
echo 6. Подключить Worker к VPS из секретного файла
set /p choice=Выберите действие: 
if "%choice%"=="1" python -m studio doctor
if "%choice%"=="2" python scripts\setup_env.py
if "%choice%"=="3" python -m studio worker
if "%choice%"=="4" python -m pytest -q -p no:cacheprovider
if "%choice%"=="5" python -m studio doctor --profile deployment
if "%choice%"=="6" python scripts\connect_worker.py --apply
pause
