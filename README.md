# Do Cats Eat Bats (dceb)

Превращение рутованного Android-смартфона в личный аппаратный мультитул
с клиент-серверной архитектурой: твердотельная часть на телефоне,
ИИ-анализ (модель `capricorn` через `arenaaispofandpars`) — на домашнем ПК.

> **Только для собственных устройств и авторизованного тестирования.**
> HID-ввод, ИК-передача и чтение NFC-чужих меток без согласия владельца
> незаконны. Серверные действия выполняются только после явного
> подтверждения в UI (никакого silent auto-execute).

## ЭТАП 1. Маппинг функций доноров

Доноры в корне репозитория: `Projector Remote Control_2.3.apk` (ИК-пульт
проектора) и `app-release.apk` (использует `android.hardware.ConsumerIrManager`
и встроенную базу `assets/Flipper-IRDB-main/**/*.ir`).

| Функция из донора | Реализация в DCEB |
|---|---|
| `ConsumerIrManager.hasIrEmitter / transmit()` | `ir/IrTransmitter.kt` (основной путь) |
| `cmd consumer_ir transmit <freq> <pattern>` через root | `ir/IrTransmitter.kt` (fallback через `RootShell`) |
| База `.ir` Flipper IRDB (parsed-записи `protocol/address/command`) | `ir/IrParser.kt` — парсинг + энкодеры NEC/NECext/Samsung32/RC5/RC6/SIRC в raw-массивы мкс; на ПК — `pc_server/irdb.py` + `GET /api/v1/irdb/*` |
| Raw-записи `.ir` (`frequency`, `duty_cycle`, `data[]`) | `ir/IrParser.kt` (type: raw используется как есть) |
| Pakетная отправка сигналов | поштучная отправка с подтверждением в UI (намеренно без «массового выключения экранов») |
| ConfigFS USB gadget (`/config/usb_gadget`, `/dev/hidg*`) | `usb/HidGadget.kt` |
| Скрипты автоматизации (DuckyScript) | `usb/DuckyInterpreter.kt` — конвертация в HID-сканкоды, запись в `/dev/hidg0` |
| USB-UART внешние модули (CC1101/ESP32/RFID) | `otg/UsbSerialBridge.kt` (`/dev/ttyUSB*`, `/dev/ttyACM*`) |
| Встроенный NFC (UID, сектора) | `nfc/NfcTool.kt` |
| ИИ-сервер с estructured JSON | `pc_server/server.py` + `ai_bridge.py` (модель `capricorn`, strict schema в `schemas.py`) |
| Режимы ИИ + память диалога | `ai_bridge.py` (`MODES`, `build_system_prompt`, история в запросе) |
| Локальная база знаний (grounding) | `knowledge.py` — 24 справочные заметки, `GET /api/v1/knowledge/search` |
| Офлайн-анализатор на все типы дампов | `OfflineAnalyzer` в `ai_bridge.py` (IR, dmesg, console, NFC, Sub-GHz, Bluetooth, сеть, hardening, CTF) |
| Авторизованный сетевой инвентарь | `netscan.py` + `POST /api/v1/netscan` (только connect()-пробы, без скрытности) |
| Пассивный Bluetooth-инвентарь | `core/BtInventory.kt` (BLE-скан + bonded, без спаривания и подключения) |
| Оценка качества ответов | `evals/` — 14 размеченных сценариев + `.venv/bin/python evals/run_eval.py` |

## Дерево проекта

```
pc_server/
  server.py          FastAPI: /api/v1/health (с живым probe модели), /process,
                     /parse_dump, /netscan (авторизованный инвентарь),
                     /dumps/upload, /session/reset, /knowledge/search,
                     /irdb/* (search с фильтром category, /irdb/categories) +
                     WebSocket /ws/v1/ai (стриминговый AI-чат с историей)
  tests/             pytest: схемы, режимы, офлайн-анализатор, netscan, сессии,
                     smoke API (запуск: .venv/bin/python -m pytest tests/)
  evals/             сценарии + скоринг: .venv/bin/python evals/run_eval.py
  ai_bridge.py       клиент arenaaispofandpars (capricorn) + режимы + OfflineAnalyzer
  knowledge.py       локальная справочная база для grounding промпта
  netscan.py         авторизованный сканер (scope-guard + connect()-пробы)
  schemas.py         pydantic-схемы строгого JSON-ответа
  irdb.py            индекс локальной базы .ir и папки dumps/
  tools/import_projector_db.py  конвертер projector.db (из Projector Remote
                     Control_2.3.apk) -> .ir файлы в irdb/Projectors/
  requirements.txt   fastapi, uvicorn, pydantic, requests
  backend.bat        Windows-лаунчер: venv, deps, adb reverse, IP, restart-loop
  dumps/ irdb/ logs/ (gitignored, кроме .gitkeep)
android_client/
  app/src/main/java/com/dceb/
    core/    RootShell (libsu), HardwareProbe, NetworkClient (ADB/LAN/Tailscale,
             session_id + история в каждом запросе), BtInventory (пассивный BLE-скан)
    ir/      IrParser (.ir + протоколы), IrTransmitter (ConsumerIrManager + cmd)
    usb/     HidGadget (ConfigFS), DuckyInterpreter (HID-сканкоды)
    otg/     UsbSerialBridge
    nfc/     NfcTool
    ai/      AiViewModel (чат + подтверждение действий сервера, executePending)
    ui/      Compose-экраны: AI-терминал — главная вкладка (пульс-анимация),
             IR-вкладка с чипами категорий (Projectors первая), HID, Device, Net
```

## Быстрый старт

ПК: `cd pc_server && backend.bat` (создаст `.venv`, пробресит `adb reverse
tcp:8000 tcp:8000`, покажет LAN/Tailscale IP). Телефон: открой приложение,
в «Net» выбери канал `ADB_REVERSE` (или введи LAN/Tailscale URL), на дашборде
нажми «Ping PC server».

Примечание: репозиторий `arenaaispofandpars` склонирован в корень как папка
`arenaaispofandpars/` (в .gitignore, в проект не коммитится). Импортируемый
пакет — `arena_api`; `pc_server/ai_bridge.py` находит репозиторий автоматически
(или через `DCEB_ARENA_REPO`) и использует `arena_api.auto_client()` +
`client.complete(prompt, model="capricorn")`. На ПК один раз выполни
`arena-api --login` (или `python -m arena_api --login`), чтобы сохранилась
сессия — без неё сервер работает в offline-fallback режиме.

## База сигналов (актуальная)

`pc_server/irdb/` — **2117 `.ir`-файлов** (~23 МБ):
- вся Flipper-IRDB из `app-release.apk` (43 категории: TVs, ACs, Projectors,
  Fans, Audio...); категория «ИК глушилка» из донора **намеренно исключена**;
- все проекторы из `Projector Remote Control_2.3.apk`: `assets/projector.db`
  (таблица `remote`, 7101 сигнал, 63 бренда) сконвертированы в
  `irdb/Projectors/<Brand>/<Model>.ir` скриптом `tools/import_projector_db.py`.

Проверено на живом сервере: `/api/v1/health` → `irdb_entries: 2067`,
`/api/v1/irdb/categories` → 43 категории, поиск `epson` / `3M Projector` и
fetch `.ir`-файла работают, `pytest tests/` → 62 passed,
`.venv/bin/python evals/run_eval.py` → 14/14 (100%) на офлайн-анализаторе.

## Медиа Capri

`capri_media/` — 4 фото (webp) и 4 видео (mp4) персонажа «Capri», скачаны из
коммита `54d41f9` ветки `main` (в нашей ветке лежат untracked). В клиенте
лежат копии в `android_client/app/src/main/res/drawable/` (`capri_avatar.webp`,
`capri_splash.webp`) — аватар с «дышащей» анимацией на главной AI-вкладке.

## Обновления клиента (ветка arena/34b2cf48-dceb)

- **WebSocket-чат (пункт 3)**: `NetworkClient.connectWs()` → `/ws/v1/ai` (OkHttp WS),
  ответы стримятся в чат; индикатор WS/REST в шапке AI-вкладки; при обрыве —
  автоматический фолбэк на REST (`/api/v1/process`).
- **История действий (пункт 2)**: `AiViewModel.history` — все proposed/approved/
  rejected/повторённые действия; в UI — чипы с кнопкой повтора (user-initiated).
- **Медиа Capri**: кадры видео из `capri_media/frames/` — промо-ролики персонажа
  (неоновый glitch-аватар в чат-UI), используются как аватар AI-вкладки.

## Сборка APK «Do Cats Eat Bats»

На ПК с JDK 17 + Android SDK:

```
cd android_client
./gradlew assembleDebug        # Linux/macOS
gradlew.bat assembleDebug      # Windows
```

Готовый APK: `android_client/app/build/outputs/apk/debug/app-debug.apk`
(имя приложения — «Do Cats Eat Bats», см. `res/values/strings.xml`). Gradle
обёрнут в wrapper (`gradlew` + `gradle/wrapper/`, Gradle 8.10.2) — отдельная
установка Gradle не нужна. Установка на рутованный телефон:
`adb install app/build/outputs/apk/debug/app-debug.apk`.

Самопроверка в этом sandbox: `pytest tests/` → 9 passed, WS `/ws/v1/ai` отвечает,
Kotlin-файлы проверены на баланс скобок (Android SDK здесь недоступен,
поэтому APK здесь не собирался — собирайте на своём ПК по инструкции выше).

## Обучение Capri: режимы, память, оценка

Capri — не вес модели, а обёртка над `capricorn`: здесь меняются промпт,
grounding, маршрутизация и фолбэк. Что изменилось:

**Режимы (`MODES`).** `analyze_ir`, `hid_help`, `dmesg`, `console`, `nfc`,
`subghz`, `bluetooth`, `netscan`, `hardening`, `ctf`, `chat`. Режим
определяется по полю `mode` (и по `dump_type`), а лишние префиксы вроде
`dump:ir` откусываются — у каждого режима свой блок инструкций и свои
справочные заметки из `knowledge.py`.

**Память диалога.** Клиент шлёт `session_id` и последние 20 реплик, сервер
держит свою копию транскрипта (`_SESSIONS`) и склеивает историю в промпт.
Кнопка «Clear memory» в AI-терминале сбрасывает разговор на обеих сторонах
(`POST /api/v1/session/reset`).

**Честный health.** `/api/v1/health` теперь делает реальный probe модели
(кеш 30 с) и отдаёт `ai_probe_ok` + `ai_last_error`, а не только факт, что
клиент создан. `?probe=false` — быстрый вариант без запроса к модели.

**Офлайн-анализатор.** Без сессии `arena_api` сервер не «тупит», а разбирает
дамп детерминированно по режиму: NEC-подобные тайминги, ключевые слова ядра,
UID NFC, Sub-GHz диапазон, Bluetooth-инвентарь, сетевой инвентарь, чеклист
харденинга, CTF-подсказки по кодировкам.

**Оценка качества.** `evals/scenarios.py` — 14 размеченных сценариев
(ожидаемый `action` + подстрока). `.venv/bin/python evals/run_eval.py` —
детерминированный прогон, `--live` добавляет реальную модель.

### Авторизованный сетевой инвентарь

`POST /api/v1/netscan` и карточка «Авторизованное сканирование сети» на
вкладке Device. scope-guard жёсткий:

- без `confirm_owner: true` → `403`;
- цель вне приватных диапазонов (RFC1918/loopback/link-local) → `403`, нужен
  отдельный `allow_public: true` и предупреждение в логе;
- только обычные `connect()`-пробы и опциональный ICMP-ping: никаких
  FIN/XMAS/NULL-сканов, фрагментации, декойов, обхода IDS, эксплойтов,
  перебора паролей и латерального движения;
- лимиты: до 256 хостов, до 128 портов, таймаут 0.4 с, пул на 32 потока.

Ответ — инвентарь (живые хосты, открытые порты, опциональные баннеры) плюс
`advice` от Capri в режиме `netscan`.

### Bluetooth-инвентарь

`core/BtInventory.kt` + карточка на вкладке Device: BLE-скан 5 секунд и
уже сопряжённые устройства. Пассивное наблюдение — имя, MAC, RSSI, класс.
Никакого спаривания, подключения или чтения данных с устройств: это требует
согласия владельца. Результат отправляется Capri в режиме `bluetooth`
(`bt_inventory`, advisory-only).

### Модель безопасности ответов

- `transmit_ir`, `execute_hid`, `ui_action` — единственные исполняемые типы,
  и только после тапа Approve.
- `net_inventory`, `bt_inventory`, `hardening`, `ctf_answer`, `info_only` —
  только советы, клиент их не исполняет.
- `run_shell` всегда понижается до `info_only`; HID из анализа дампов
  понижается до `info_only` (кроме режима `hid`).
- Промпт содержит явный AUTHORIZATION SCOPE: только свои устройства или
  стенды с разрешением, без скрытности и без выноса данных.

## UI-автоматизация телефона (своего)

`core/UiAutomationService.kt` (AccessibilityService): чтение экрана (`read_screen`),
тап по тексту (`tap_text`), тап по координатам (`tap_xy`), свайп (`swipe`),
запуск приложения (`open_app`). Включается в «Настройки → Спец. возможности»
(кнопка на вкладке Device). Сервер может предлагать `action: "ui_action"` —
выполнение строго за кнопкой Approve, только на своём устройстве.
