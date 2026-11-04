@echo off
chcp 65001 >nul
title Do Cats Eat Bats - Capricorn AI Backend Server
color 0A

set "PORT=8000"

echo ========================================================
echo    DO CATS EAT BATS :: PC SERVER LAUNCHER (CAPRICORN)
echo ========================================================
echo.

:: 1. Проверка наличия Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    color 0C
    echo [ERROR] Python не найден в PATH! Установи Python 3.10+ и отметь "Add to PATH".
    pause
    exit /b 1
)
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do echo [OK] Python %%v

:: 2. Создание виртуального окружения при первом запуске
if not exist ".venv" (
    echo [INIT] Создание виртуального окружения .venv...
    python -m venv .venv
    if %errorlevel% neq 0 (
        color 0C
        echo [ERROR] Не удалось создать .venv
        pause
        exit /b 1
    )
)

:: 3. Активация окружения
call .venv\Scripts\activate.bat

:: 4. Базовые папки для дампов и баз Flipper
if not exist "irdb" mkdir irdb
if not exist "dumps" mkdir dumps
if not exist "logs" mkdir logs

:: 5. Зависимости
if not exist "requirements.txt" (
    echo [INIT] Генерация requirements.txt...
    (
        echo fastapi^>=0.110
        echo uvicorn[standard]^>=0.29
        echo pydantic^>=2.6
        echo requests^>=2.31
    ) > requirements.txt
)
echo [DEPS] Установка/обновление зависимостей...
pip install -r requirements.txt --quiet --disable-pip-version-check
if exist "requirements-local.txt" (
    echo [DEPS] Установка приватных зависимостей из requirements-local.txt...
    pip install -r requirements-local.txt --quiet --disable-pip-version-check
)

:: 6. ADB reverse (телефон на кабеле -> 127.0.0.1:8000 на телефоне)
echo.
echo [USB] Проверка подключения Android по ADB...
adb version >nul 2>&1
if %errorlevel% equ 0 (
    adb get-state 1>nul 2>&1
    if %errorlevel% equ 0 (
        adb reverse tcp:%PORT% tcp:%PORT%
        if %errorlevel% equ 0 (
            echo [OK] ADB-туннель активен! Телефон стучится на http://127.0.0.1:%PORT%
        ) else (
            echo [WARN] adb reverse не выполнен.
        )
    ) else (
        echo [INFO] Устройство не в состоянии device. Используй LAN или Tailscale.
    )
) else (
    echo [INFO] ADB не найден в PATH. Пропуск USB-туннеля.
)

:: 7. Сетевые адреса: LAN IPv4 + Tailscale
echo.
echo [NETWORK] Адреса этого ПК (порт %PORT%):
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    set "ip=%%a"
    echo   - LAN: http://%ip%:8000
)
tailscale ip -4 2>nul
if %errorlevel% equ 0 (
    for /f %%t in ('tailscale ip -4 2^>nul') do echo   - Tailscale: http://%%t:%PORT%
) else (
    echo   - Tailscale: не обнаружен ^(установи клиент, если нужен удаленный доступ^)
)
echo ========================================================
echo.

:: 8. Запуск uvicorn с автоперезапуском
:server_loop
echo [START] FastAPI (Capricorn AI Bridge) -> http://0.0.0.0:%PORT%
python -m uvicorn server:app --host 0.0.0.0 --port %PORT% --reload
if %errorlevel% equ 0 goto :eof_clean

echo.
echo [WARN] Сервер упал (код %errorlevel%). Перезапуск через 3 сек. Дважды Ctrl+C — выход.
timeout /t 3 /nobreak >nul
goto server_loop

:eof_clean
echo [STOP] Сервер остановлен штатно.
