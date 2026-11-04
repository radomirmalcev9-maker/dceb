@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title Do Cats Eat Bats :: STARTSERV
color 0A

rem =====================================================================
rem  startserv.bat — один запуск, и всё поднято.
rem
rem  Что делает:
rem    1. находит Python, создаёт pc_server\.venv, ставит зависимости
rem    2. создаёт irdb\ dumps\ logs\ archives\
rem    3. если база ИК пуста — импортирует доноров из корня репозитория
rem    4. поднимает adb reverse (телефон на кабеле)
rem    5. печатает LAN/Tailscale адреса
rem    6. быстрый импорт-тест, затем uvicorn с автоперезапуском
rem
rem  Использование:
rem    startserv.bat                порт 8000, открыть /docs в браузере
rem    startserv.bat 9000           другой порт
rem    startserv.bat 9000 nobrowser без браузера
rem =====================================================================

set "ROOT=%~dp0"
set "SRV=%ROOT%pc_server"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"
set "OPENBROWSER=1"
if /i "%~2"=="nobrowser" set "OPENBROWSER=0"

echo ========================================================
echo    DO CATS EAT BATS :: STARTSERV  (port %PORT%)
echo ========================================================
echo.

rem ---------------------------------------------------------------- 1. Python
set "PY=python"
%PY% --version >nul 2>&1
if errorlevel 1 (
    set "PY=py -3"
    py -3 --version >nul 2>&1
    if errorlevel 1 (
        color 0C
        echo [ERROR] Python не найден ни как python, ни как py -3.
        echo         Установи Python 3.10+ с галочкой "Add to PATH".
        pause
        exit /b 1
    )
)
for /f "tokens=*" %%v in ('%PY% --version 2^>^&1') do echo [OK] %%v

if not exist "%SRV%" (
    color 0C
    echo [ERROR] Не вижу папку pc_server рядом с батником: %SRV%
    pause
    exit /b 1
)
cd /d "%SRV%"

rem ------------------------------------------------------------------ 2. venv
if not exist ".venv\Scripts\python.exe" (
    echo [INIT] Создаю виртуальное окружение .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        color 0C
        echo [ERROR] Не удалось создать .venv
        pause
        exit /b 1
    )
)
set "VPY=%SRV%\.venv\Scripts\python.exe"
if not exist "%VPY%" (
    color 0C
    echo [ERROR] .venv собран криво, нет python.exe
    pause
    exit /b 1
)
echo [OK] venv: %VPY%

rem ------------------------------------------------------------- 3. каталоги
for %%D in (irdb dumps logs archives) do (
    if not exist "%%D" mkdir "%%D"
    if not exist "%%D\.gitkeep" type nul > "%%D\.gitkeep"
)
echo [OK] Каталоги: irdb, dumps, logs, archives

rem -------------------------------------------------------- 4. зависимости
echo [DEPS] Ставлю/обновляю зависимости (тихо)...
"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%VPY%" -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
    color 0E
    echo [WARN] Часть зависимостей не встала — сервер может работать в офлайне.
)
if exist "requirements-local.txt" (
    echo [DEPS] Приватные зависимости из requirements-local.txt ...
    "%VPY%" -m pip install -r requirements-local.txt --quiet --disable-pip-version-check
)

rem ------------------------------------------------- 5. модель: arena_api
if exist "%ROOT%arenaaispofandpars\arena_api\__init__.py" (
    set "DCEB_ARENA_REPO=%ROOT%arenaaispofandpars"
    echo [AI] Найден arenaaispofandpars: !DCEB_ARENA_REPO!
) else (
    echo [AI] Рядом нет arenaaispofandpars — Capri будет в offline-fallback.
)
"%VPY%" -c "import arena_api" >nul 2>&1
if errorlevel 1 (
    echo [AI] arena_api не импортируется: ответы пойдут от офлайн-анализатора.
    echo      Чтобы включить capricorn: arena-api --login  (один раз на этом ПК)
) else (
    echo [AI] arena_api на месте, модель: %DCEB_MODEL%
)

if "%DCEB_MODEL%"=="" set "DCEB_MODEL=capricorn"
if "%DCEB_ARCHIVE_ROOT%"=="" set "DCEB_ARCHIVE_ROOT=%SRV%\archives"
if "%DCEB_ARCHIVE_RETENTION_DAYS%"=="" set "DCEB_ARCHIVE_RETENTION_DAYS=30"

rem ------------------------------------------------- 6. база ИК-сигналов
set "IRCOUNT=0"
for /f %%C in ('dir /b /s "irdb\*.ir" 2^>nul ^| find /c /v ""') do set "IRCOUNT=%%C"
if "!IRCOUNT!"=="0" (
    echo [IRDB] База пуста — импортирую доноров из корня репозитория...
    if exist "%ROOT%app-release.apk" (
        echo [IRDB]   Flipper IRDB из app-release.apk ...
        "%VPY%" tools\import_flipper_irdb.py
    ) else (
        echo [IRDB]   нет app-release.apk — пропуск Flipper IRDB
    )
    if exist "%ROOT%Projector Remote Control_2.3.apk" (
        echo [IRDB]   Проекторы из Projector Remote Control_2.3.apk ...
        "%VPY%" tools\import_projector_db.py
    ) else (
        echo [IRDB]   нет APK проектора — пропуск Projectors
    )
    set "IRCOUNT=0"
    for /f %%C in ('dir /b /s "irdb\*.ir" 2^>nul ^| find /c /v ""') do set "IRCOUNT=%%C"
)
echo [OK] IRDB: !IRCOUNT! файлов .ir

rem ------------------------------------------------------- 7. быстрый тест
echo [TEST] Проверяю, что сервер импортируется...
"%VPY%" -c "import server, schemas, netscan, archive, knowledge, ai_bridge; print('[TEST] импорты ok')"
if errorlevel 1 (
    color 0C
    echo [ERROR] Сервер не импортируется. Смотри текст ошибки выше.
    pause
    exit /b 1
)

rem ------------------------------------------------------------- 8. adb
echo.
echo [USB] Ищу Android по ADB...
where adb >nul 2>&1
if errorlevel 1 (
    echo [USB] adb нет в PATH — пропуск (используй LAN или Tailscale).
) else (
    adb get-state >nul 2>&1
    if errorlevel 1 (
        echo [USB] Устройство не в состоянии device — пропуск туннеля.
    ) else (
        adb reverse tcp:%PORT% tcp:%PORT%
        if errorlevel 1 (
            echo [USB] adb reverse не удался.
        ) else (
            echo [OK] ADB-туннель активен: телефон стучится на http://127.0.0.1:%PORT%
        )
    )
)

rem ------------------------------------------------------------ 9. адреса
echo.
echo [NETWORK] Адреса этого ПК (порт %PORT%):
set "FOUNDLAN=0"
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    set "ip=%%a"
    set "ip=!ip: =!"
    if not "!ip!"=="" (
        echo   - LAN: http://!ip!:%PORT%
        set "FOUNDLAN=1"
    )
)
if "!FOUNDLAN!"=="0" echo   - LAN: адрес не найден
where tailscale >nul 2>&1
if errorlevel 1 (
    echo   - Tailscale: не установлен
) else (
    for /f "delims=" %%t in ('tailscale ip -4 2^>nul') do echo   - Tailscale: http://%%t:%PORT%
)
echo ========================================================
echo    health: http://127.0.0.1:%PORT%/api/v1/health
echo    docs:   http://127.0.0.1:%PORT%/docs
echo    STOP:   дважды Ctrl+C
echo ========================================================
echo.

if "%OPENBROWSER%"=="1" start "" http://127.0.0.1:%PORT%/docs

rem -------------------------------------------------------- 10. сам сервер
:server_loop
echo [START] uvicorn server:app -> http://0.0.0.0:%PORT%
"%VPY%" -m uvicorn server:app --host 0.0.0.0 --port %PORT% --reload
echo.
echo [WARN] Сервер остановился (код %ERRORLEVEL%). Перезапуск через 3 сек.
echo       Чтобы выйти — закрой окно или нажми Ctrl+C дважды.
timeout /t 3 /nobreak >nul
goto server_loop
