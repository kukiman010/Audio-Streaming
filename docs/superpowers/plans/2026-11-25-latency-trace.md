# Трассировка задержки Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Кнопка «Замеры» в `gui_client.py` запускает/останавливает трассировку задержки: стример раз в секунду шлёт ping по data channel (канал данных LiveKit), слушатели отвечают, а нижняя панель показывает разбивку по этапам.

**Architecture:** Чистая логика (протокол пакетов, расчёт, форматирование) живёт в новом `latency_trace.py` и тестируется unittest-ом. `LiveKitStreamClient` получает `start_trace`/`stop_trace` и цикл ping, GUI только вызывает их и рисует строки. Веб-страница и Android отвечают на `lat.ping` пакетом `lat.pong`.

**Tech Stack:** Python 3.11, `livekit` rtc SDK (`Room.get_rtc_stats`, `publish_data`, событие `data_received`), tkinter, aiohttp (`server.py`), livekit-client JS, livekit-android 2.26.1 (Kotlin), unittest.

**Spec:** `docs/superpowers/specs/2026-11-25-latency-trace-design.md`

## Global Constraints

- Ping раз в секунду, `reliable=False`, `topic="lat.ping"`; ответ `topic="lat.pong"`, адресно стримеру (`destination_identities`).
- Формат пакетов: JSON с полем `"v": 1`. Ping: `{"v":1,"id":int,"t":float,"pub_rtt_ms":float|null}`. Pong: `{"v":1,"id":int,"sfu_rtt_ms":float|null,"jitter_buffer_ms":float|null}`. `t` — `time.monotonic()*1000` стримера.
- Формулы: `сервер↔клиент = max(0, круг − до_сервера)`; `оценка = (до_сервера + сервер↔клиент)/2 + буфер_клиента`. Клиент считает так же: `pub_rtt/2 + sfu_rtt/2 + jitter_buffer`.
- Нет ответа слушателя: подпись «нет ответа (обновите клиент)». Нет данных поля: «н/д».
- Выключенная трассировка не шлёт ping и не опрашивает статистику. Аудиотракт не меняется. Только транспорт LiveKit.
- Клиент гасит показ оценки, если ping нет 3 с.
- Тип соединения (UDP/TCP/relay) показывается один раз при старте трассировки.
- Тесты: стандартный `unittest`, запуск `python -m unittest discover -s tests -v` из корня репозитория.

## Review Focus

- Pong со старым `id`, после остановки трассировки или от неизвестного участника: игнорируется, без исключений.
- Чужой/битый data-пакет (не JSON, другой topic, другой `v`): игнорируется.
- В комнате нет слушателей: строка «слушателей нет», трассировка продолжается.
- Обрыв связи или hard reconnect во время трассировки: трассировка останавливается, кнопка возвращается в исходное состояние.
- Замеры не стирают ошибки, ошибки не стирают замеры (панель обновляется раз в секунду из `_render_livekit_state`); строк замеров не больше 200.

---

## File Structure

- Create `latency_trace.py`: протокол, расчёт, форматирование, `TraceStats`, `compose_log`.
- Create `tests/test_latency_trace.py`.
- Modify `livekit_client.py`: `start_trace`, `stop_trace`, цикл, обработчик `data_received`, извлечение RTT и типа соединения из статистики.
- Modify `gui_client.py`: кнопка, адаптер, панель.
- Modify `server.py`: JS страницы слушателя.
- Modify `android/app/src/main/java/com/audiostreaming/listener/PlaybackService.kt` (+ новый `LatencyTrace.kt` для чистой логики).
- Modify `docs/architecture.md`.

---

### Task 1: Чистая логика `latency_trace.py`

**Files:**
- Create: `latency_trace.py`
- Test: `tests/test_latency_trace.py`

**Interfaces:**
- Produces:
  - `TOPIC_PING = "lat.ping"`, `TOPIC_PONG = "lat.pong"`, `MAX_LOG_LINES = 200`
  - `encode_ping(ping_id: int, t_ms: float, pub_rtt_ms: Optional[float]) -> bytes`
  - `decode_pong(data: bytes) -> Optional[Pong]`; `Pong` — dataclass `(id: int, sfu_rtt_ms: Optional[float], jitter_buffer_ms: Optional[float])`; любой мусор или `v != 1` даёт `None`
  - `Stages` — dataclass `(pub_rtt_ms: Optional[float], loop_ms: float, sfu_client_ms: Optional[float], buffer_ms: Optional[float], total_ms: Optional[float])`
  - `compute_stages(pub_rtt_ms: Optional[float], loop_ms: float, buffer_ms: Optional[float]) -> Stages`
  - `estimate_ms(pub_rtt_ms: float, sfu_rtt_ms: float, buffer_ms: float) -> float`
  - `format_line(identity: str, stages: Optional[Stages]) -> str` (`None` — нет ответа)
  - `class TraceStats`: `add(identity: str, stages: Stages) -> None`, `summary_lines() -> list[str]` (min/avg/max по `total_ms` и по каждому этапу, на слушателя)
  - `compose_log(error_text: str, trace_lines: list[str]) -> str` — ошибка сверху, затем последние `MAX_LOG_LINES` строк замеров

- [ ] **Step 1: Write the failing tests** in `tests/test_latency_trace.py`:
  - `test_compute_stages_example`: `compute_stages(62, 147, 940)` → `sfu_client_ms == 85`, `total_ms == (62+85)/2 + 940`.
  - `test_loop_smaller_than_pub_rtt_clamps_to_zero`: `compute_stages(100, 80, 10).sfu_client_ms == 0`.
  - `test_missing_pub_rtt`: `compute_stages(None, 147, 940)` → `sfu_client_ms is None`, `total_ms is None`; `format_line` содержит «до сервера н/д».
  - `test_decode_pong_garbage`: `decode_pong(b"\xff")`, `decode_pong(b"{}")`, `decode_pong(b'{"v":2,"id":1}')`, `decode_pong(b'{"v":1,"id":"x"}')` → все `None`.
  - `test_decode_pong_ok`: `decode_pong(b'{"v":1,"id":7,"sfu_rtt_ms":50,"jitter_buffer_ms":900}')` → `Pong(7, 50.0, 900.0)`.
  - `test_encode_ping_roundtrip`: JSON-разбор результата содержит `v=1`, `id`, `t`, `pub_rtt_ms`.
  - `test_format_line_no_answer`: `format_line("a", None)` содержит «нет ответа (обновите клиент)».
  - `test_trace_stats_summary`: два `add` для одного слушателя → в сводке min/avg/max по `total_ms`.
  - `test_compose_log_keeps_both_and_caps`: ошибка присутствует в начале, при 300 строках замеров остаётся последних 200; пустая ошибка не даёт пустой первой строки.

- [ ] **Step 2: Run** `python -m unittest discover -s tests -v`. Expected: FAIL (`ModuleNotFoundError: latency_trace`).

- [ ] **Step 3: Implement** всё из Interfaces в `latency_trace.py` (только стандартная библиотека). Строка по образцу из спеки: `[замер] android-viewer: до сервера 62 мс | сервер↔клиент 85 мс | буфер клиента 940 мс | ≈ 1,0 с` (секунды с одним знаком и запятой).

- [ ] **Step 4: Run** the same command. Expected: PASS.

- [ ] **Step 5: Commit** `feat: latency trace protocol and formatting`.

---

### Task 2: Цикл трассировки в `livekit_client.py`

**Files:**
- Modify: `livekit_client.py` (класс `LiveKitStreamClient`, `_register_room_handlers`, `stop`)
- Modify: `latency_trace.py` (две функции извлечения из статистики)
- Test: `tests/test_latency_trace.py`

**Interfaces:**
- Consumes: всё из Task 1.
- Produces:
  - `extract_pub_rtt_ms(stats: "rtc.RtcStats") -> Optional[float]` и `extract_conn_type(stats: "rtc.RtcStats") -> str` (`"UDP"`, `"TCP"`, `"relay (TURN)"` или `"н/д"`) в `latency_trace.py`
  - `LiveKitStreamClient.trace_callback: Optional[Callable[[str], None]]`: вызывается для каждой готовой строки панели и для сводки
  - `async LiveKitStreamClient.start_trace() -> None`, `async LiveKitStreamClient.stop_trace() -> None`, свойство `tracing: bool`

- [ ] **Step 1: Write the failing tests**:
  - `test_extract_pub_rtt_from_real_proto`: собрать `RtcStats` из `livekit.rtc._proto` (см. `livekit/rtc/room.py:143` и `_proto/stats_pb2`), где `remote_inbound_rtp` аудио-трека имеет RTT, и убедиться, что функция возвращает его в миллисекундах; пустой список → `None`.
  - `test_extract_conn_type`: пара кандидатов `relay` → `"relay (TURN)"`, `udp` host → `"UDP"`, пусто → `"н/д"`.
  - Какие именно поля protobuf использовать, определить по `stats_pb2` и по реальному `get_rtc_stats()` на живом подключении; если названия полей в тесте и в коде разойдутся с реальностью, победит реальная статистика.

- [ ] **Step 2: Run** unittest. Expected: FAIL (функции не определены).

- [ ] **Step 3: Implement** функции извлечения в `latency_trace.py`.

- [ ] **Step 4: Implement** в `LiveKitStreamClient`:
  - в `_register_room_handlers` добавить `room.on("data_received", self._on_data_received)`; обработчик проверяет `packet.topic == TOPIC_PONG`, `decode_pong(packet.data)`, игнорирует пакеты, если трассировка выключена, `id` неизвестен или участник не из комнаты; считает `loop_ms = now_ms − t_sent`, `compute_stages`, пишет `format_line` в `trace_callback` и в `TraceStats` (поля пакета `data`/`participant.identity`/`topic` сверить с `room.py:886`).
  - `start_trace`: ничего не делает, если нет `self.room` или фаза не `connected`; шлёт в `trace_callback` один раз тип соединения; запускает задачу `_trace_loop`: раз в 1 с берёт `room.get_rtc_stats()`, вычисляет `pub_rtt`, `publish_data(encode_ping(...), reliable=False, topic=TOPIC_PING)`, хранит `{id: t_sent}` и забывает записи старше 5 с; в конце такта пишет «нет ответа» для слушателей из `room.remote_participants`, не ответивших на этот ping в течение 2 тактов, и «слушателей нет», если комната пуста.
  - `stop_trace`: отменяет задачу, выводит `TraceStats.summary_lines()`.
  - `stop()` и `_set_phase`, если фаза стала не `connected`, вызывают `stop_trace`.

- [ ] **Step 5: Run** unittest. Expected: PASS.

- [ ] **Step 6: Commit** `feat: latency trace loop in livekit client`.

---

### Task 3: Кнопка и панель в `gui_client.py`

**Files:**
- Modify: `gui_client.py` (`LiveKitClientAdapter` ~642, `App` ~681: кнопки ~834, панель ~865, `_set_error_log` ~976, `_render_livekit_state` ~1278, `on_stop` ~1246)

**Interfaces:**
- Consumes: `start_trace`/`stop_trace`/`trace_callback`/`tracing` (Task 2), `compose_log` (Task 1).
- Produces: `LiveKitClientAdapter.start_trace()`, `.stop_trace()`; `App.btn_trace`, `App.on_trace_toggle()`, `App._append_trace_line(text: str)`.

- [ ] **Step 1: Implement** в адаптере: `start_trace`/`stop_trace` через `asyncio.run_coroutine_threadsafe(...)`, и передача `trace_callback` в `LiveKitStreamClient` так, чтобы строки попадали в `App` через `self.after(0, ...)` (как `on_livekit_state_update`).
- [ ] **Step 2: Implement** `btn_trace` (`ttk.Button`, текст «Замеры» / «Остановить замеры») в `row=12, column=0`; `on_trace_toggle` переключает состояние; кнопка `disabled`, пока не `phase == "connected"`.
- [ ] **Step 3: Implement** хранение `self._error_text` и `self._trace_lines`; `_set_error_log(text)` сохраняет текст ошибки и перерисовывает панель через `compose_log`; `frm_errors` виден, если есть ошибка или строки замеров. `_append_trace_line` дописывает и перерисовывает. При остановке стримера или потере связи трассировка останавливается, а подпись кнопки возвращается.
- [ ] **Step 4: Verify manually**: запустить `python gui_client.py`, подключиться к LiveKit, нажать «Замеры»: в панели появляются строки раз в секунду; ошибка в `_error_text` (например, остановить сервер) не исчезает через секунду; повторное нажатие даёт сводку и возвращает подпись. Expected: всё перечисленное работает, без исключений в консоли.
- [ ] **Step 5: Commit** `feat: trace button and log panel`.

---

### Task 4: Веб-слушатель в `server.py`

**Files:**
- Modify: `server.py:106-158` (JS внутри f-строки: все `{`/`}` удваиваются)
- Test: `tests/test_server_page.py`

**Interfaces:**
- Consumes: протокол пакетов (Global Constraints).
- Produces: на странице строка `id="latency"` с текстом «≈ N мс (оценка)» и обработчик пакетов `lat.ping`.

- [ ] **Step 1: Write the failing test** `test_page_has_latency_handler`: получить HTML страницы (через `aiohttp.test_utils` или вызов обработчика главной страницы) и проверить, что есть строки `lat.ping`, `lat.pong`, `id="latency"`.
- [ ] **Step 2: Run** unittest. Expected: FAIL.
- [ ] **Step 3: Implement** в JS: после подключения `lkRoom.on(RoomEvent.DataReceived, (payload, participant, kind, topic) => …)` (импортировать `RoomEvent`); для `topic === "lat.ping"` разобрать JSON, ответить `lkRoom.localParticipant.publishData(bytes, {{reliable: false, topic: "lat.pong", destinationIdentities: [participant.identity]}})`; значения `sfu_rtt_ms` и `jitter_buffer_ms` брать из `getRTCStatsReport()` подписанного аудиотрека (RTT из `candidate-pair`, буфер как `jitterBufferDelay / jitterBufferEmittedCount * 1000` из `inbound-rtp`); поля, которые не нашлись, слать `null`; показать оценку в `#latency`, гасить через 3 с без ping; битый JSON игнорировать в `try/catch`.
- [ ] **Step 4: Run** unittest. Expected: PASS.
- [ ] **Step 5: Verify manually** вместе с Task 3: открыть страницу слушателя, запустить замеры на стримере, увидеть строку слушателя в панели стримера и «≈» на странице; после остановки замеров «≈» гаснет через 3 с.
- [ ] **Step 6: Commit** `feat: web viewer answers latency ping`.

---

### Task 5: Android-слушатель

**Files:**
- Create: `android/app/src/main/java/com/audiostreaming/listener/LatencyTrace.kt`
- Modify: `android/app/src/main/java/com/audiostreaming/listener/PlaybackService.kt` (цикл `room.events.collect` ~220; уведомление ~234, ~303)
- Test: `android/app/src/test/java/com/audiostreaming/listener/LatencyTraceTest.kt` (добавить `testImplementation("junit:junit:4.13.2")` в `android/app/build.gradle.kts`, если его нет)

**Interfaces:**
- Produces в `LatencyTrace.kt`:
  - `object LatencyTrace { const val TOPIC_PING = "lat.ping"; const val TOPIC_PONG = "lat.pong" }`
  - `data class Ping(val id: Long, val pubRttMs: Double?)`
  - `fun parsePing(data: ByteArray): Ping?` (мусор, чужой `v` → `null`)
  - `fun buildPong(id: Long, sfuRttMs: Double?, jitterBufferMs: Double?): ByteArray`
  - `fun estimateMs(pubRttMs: Double, sfuRttMs: Double, bufferMs: Double): Double`

- [ ] **Step 1: Write the failing tests**: `parsePing` отвергает мусор и `v=2`; принимает `{"v":1,"id":7,"t":1.0,"pub_rtt_ms":62.0}`; `buildPong` даёт JSON с `v=1`, `id`; `estimateMs(62.0, 50.0, 900.0) == 956.0`.
- [ ] **Step 2: Run** `./gradlew :app:testDebugUnitTest` (в каталоге `android/`). Expected: FAIL.
- [ ] **Step 3: Implement** `LatencyTrace.kt` (org.json или kotlinx.serialization, что уже есть в проекте).
- [ ] **Step 4: Implement** в `PlaybackService`: ветка `is RoomEvent.DataReceived` для `topic == TOPIC_PING`: собрать `sfuRttMs` и `jitterBufferMs` из статистики аудиотрека LiveKit Android SDK (какой именно вызов статистики в 2.26.1, определить по SDK), ответить `room.localParticipant.publishData(bytes, DataPublishReliability.LOSSY, topic = TOPIC_PONG, identities = listOf(sender))`; показать оценку в тексте уведомления (`updateNotification`), вернуть обычный текст, если ping нет 3 с.
- [ ] **Step 5: Run** unit tests и `./gradlew :app:assembleDebug`. Expected: PASS, BUILD SUCCESSFUL.
- [ ] **Step 6: Verify manually** на устройстве вместе со стримером: в панели стримера строка с `android-viewer`, в уведомлении «≈ …».
- [ ] **Step 7: Commit** `feat: android viewer answers latency ping`.

---

### Task 6: Документация и сквозная проверка

**Files:**
- Modify: `docs/architecture.md` (новый раздел «Замеры задержки» перед «Проверка»)

- [ ] **Step 1: Write** раздел: что делает кнопка, формат пакетов (`lat.ping`/`lat.pong`), как читать строку, ограничение (допущение симметрии), требование к клиентам (нужна версия с ответом).
- [ ] **Step 2: Verify end-to-end**: стример + браузер + Android одновременно; замеры 30 с; в панели по строке на каждого, в сводке min/avg/max; остановить стримера в процессе — трассировка завершается сама. Затем `python -m unittest discover -s tests -v`. Expected: все тесты PASS.
- [ ] **Step 3: Commit** `docs: latency trace`.

---

## Self-Review

- Покрытие спеки: кнопка и панель (Task 3), сводка (Task 1, 2), слушатели по отдельности (Task 2), оценка у клиента (Task 4, 5), тип соединения (Task 2), «нет ответа» (Task 1, 2), документация (Task 6). Пробелов нет.
- Имена согласованы: `TOPIC_PING/PONG`, `compute_stages`, `format_line`, `compose_log`, `start_trace/stop_trace`, `trace_callback`, `_append_trace_line` везде одинаковы.
- Пропорция: план короче кода, который опишет; тела функций не приведены, кроме формул в Global Constraints.
