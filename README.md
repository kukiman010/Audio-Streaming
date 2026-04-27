# Audio Streaming (LiveKit) — Debian 12 + Windows/Linux client

Компоненты:
- `gui_client.py` — Tkinter-клиент с нативным publish в LiveKit (без ffmpeg-транспорта).
- `server.py` — helper service: web-viewer + endpoint для выдачи LiveKit токенов.
- `deploy/livekit/` — self-host инфраструктура LiveKit (Docker Compose + примеры конфигов).
- `audio_devices.py` — кроссплатформенное перечисление input-устройств (Linux/Windows).

## Установка (Debian 12)

1) Системные пакеты:
sudo apt update
sudo apt install -y python3-venv python3-pip ffmpeg portaudio19-dev

2) Сервер:
cd audio-streaming/server
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# Запуск разработки:
uvicorn server.app:app --host 0.0.0.0 --port 8000 --proxy-headers

# Продакшн через systemd:
# Скопируйте проект в /opt/audio-streaming, отредактируйте юнит (путь/пользователь), затем:
sudo cp ../systemd/audio-stream-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now audio-stream-server.service

# (Опционально) Nginx фронт + HTTPS (Let's Encrypt):
# Проверьте nginx/audio-stream.conf и установите certbot.

Переменные окружения сервера:
- STREAM_DOMAIN: если задан, ссылки строятся через этот домен (например, kukiman.ru).
- STREAM_SCHEME: http/https; если не задан — берется из запроса.
- PREROLL_MAX_BYTES: размер буфера преролла (по умолчанию 262144).
- HEADER_WAIT_TIMEOUT: таймаут ожидания заголовков для слушателя (по умолчанию 5.0).
- ACTIVE_THRESHOLD_SEC: окно, в котором поступали данные, чтобы считать “в эфире” (по умолчанию 2.5 сек).

3) Клиент-стример:
cd ../client_streamer
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 streamer.py

В окне задайте адрес сервера (например, http://127.0.0.1:8000), выберите входное устройство и нажмите “Начать стрим”.

## Быстрый старт (миграция на LiveKit)

1) Установите зависимости:
```bash
python -m pip install -r requirements.txt
python -m pip install livekit-api
```

2) Разверните LiveKit self-host:
- Используйте `deploy/livekit/docker-compose.yml`
- Скопируйте:
  - `deploy/livekit/livekit.yaml.example -> deploy/livekit/livekit.yaml`
  - `deploy/livekit/turnserver.conf.example -> deploy/livekit/turnserver.conf`
- Заполните секреты и домен, затем поднимите сервисы.

3) Задайте переменные окружения для token helper:
```bash
export LIVEKIT_API_KEY=devkey
export LIVEKIT_API_SECRET=replace_with_long_secret
```

4) Запустите helper web:
```bash
python server.py --host 0.0.0.0 --port 8000
```

5) Запустите клиент:
```bash
python gui_client.py
```

6) Проверка smoke/perf:
```bash
python performance_validation.py
```

## Советы по задержке и качеству

- Используйте UDP и рабочий TURN (coturn) для клиентов за NAT.
- Начинайте с `48000 Hz`, `1-2` канала; увеличивайте только при необходимости.
- Для production задавайте длинные секреты (`LIVEKIT_API_SECRET` 32+ байт).

## Локальное тестирование

- Запустите `server.py`.
- В браузере откройте `/` и подключитесь к room через встроенный LiveKit viewer.
- Запустите `gui_client.py` и публикуйте аудио в ту же комнату.

