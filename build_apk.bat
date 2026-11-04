@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title Do Cats Eat Bats :: BUILD APK
color 0B

rem =====================================================================
rem  build_apk.bat — собрать APK «Do Cats Eat Bats».
rem
rem  Что делает:
rem    1. проверяет JDK 17 и Android SDK (ANDROID_HOME или local.properties)
rem    2. при необходимости пишет android_client\local.properties
rem    3. зовёт gradlew и показывает, где лежит готовый APK
rem    4. по желанию сразу ставит его на подключённый телефон
rem
rem  Использование:
rem    build_apk.bat                       assembleDebug
rem    build_apk.bat assembleDebug install собрать и установить
rem    build_apk.bat installDebug          собрать и поставить одной задачей
rem    build_apk.bat assembleRelease       релиз (нужна подпись)
rem    build_apk.bat clean                 почистить build/
rem    build_apk.bat test                  unit-тесты
rem =====================================================================

set "ROOT=%~dp0"
set "CLIENT=%ROOT%android_client"
set "TASK=%~1"
if "%TASK%"=="" set "TASK=assembleDebug"
set "DOINSTALL=0"
if /i "%~2"=="install" set "DOINSTALL=1"

echo ========================================================
echo    DO CATS EAT BATS :: BUILD APK  (%TASK%)
echo ========================================================
echo.

rem ------------------------------------------------------------- 1. Java
java -version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo [ERROR] Java не найдена в PATH. Нужен JDK 17.
    echo         Проверь: java -version
    pause
    exit /b 1
)
echo [OK] Java:
java -version 2>&1 | findstr /c:"version"

rem --------------------------------------------------------- 2. Android SDK
if not exist "%CLIENT%" (
    color 0C
    echo [ERROR] Нет папки android_client рядом с батником.
    pause
    exit /b 1
)
cd /d "%CLIENT%"

if "%ANDROID_HOME%"=="" (
    if exist "%LOCALAPPDATA%\Android\Sdk" set "ANDROID_HOME=%LOCALAPPDATA%\Android\Sdk"
)
if "%ANDROID_HOME%"=="" (
    if exist "local.properties" (
        echo [OK] Есть local.properties, ANDROID_HOME не требуется.
    ) else (
        color 0C
        echo [ERROR] Не найден Android SDK.
        echo         Вариант 1: set ANDROID_HOME=C:\Users\Тебя\AppData\Local\Android\Sdk
        echo         Вариант 2: создай android_client\local.properties со строкой
        echo                    sdk.dir=C\:\\Users\\Тебя\\AppData\\Local\\Android\\Sdk
        pause
        exit /b 1
    )
) else (
    echo [OK] ANDROID_HOME=%ANDROID_HOME%
    if not exist "local.properties" (
        set "SDKLINE=!ANDROID_HOME:\=\\!"
        set "SDKLINE=!SDKLINE::=\:!"
        > local.properties echo sdk.dir=!SDKLINE!
        echo [OK] Создан local.properties: sdk.dir=!SDKLINE!
    )
)

rem ------------------------------------------------------------ 3. Сборка
echo.
echo [BUILD] gradlew %TASK% ...
call gradlew.bat %TASK%
if errorlevel 1 (
    color 0C
    echo.
    echo [ERROR] Сборка провалилась, смотри вывод Gradle выше.
    pause
    exit /b 1
)

rem ------------------------------------------------------- 4. Где результат
echo.
echo [OK] Готово. Артефакты:
if exist "app\build\outputs\apk\debug\app-debug.apk" (
    echo   debug APK: %CLIENT%\app\build\outputs\apk\debug\app-debug.apk
)
if exist "app\build\outputs\apk\release\app-release.apk" (
    echo   release APK: %CLIENT%\app\build\outputs\apk\release\app-release.apk
)

rem ----------------------------------------------------- 5. Установка (опц.)
if "%DOINSTALL%"=="1" (
    echo.
    where adb >nul 2>&1
    if errorlevel 1 (
        echo [WARN] adb нет в PATH — установи APK вручную.
    ) else (
        adb install -r "app\build\outputs\apk\debug\app-debug.apk"
        if errorlevel 1 (
            echo [WARN] adb install не удался. Телефон подключён и разблокирован?
        ) else (
            echo [OK] Установлено на устройство.
        )
    )
)
echo.
pause
