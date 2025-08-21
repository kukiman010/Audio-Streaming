# Audio Streaming (Ogg Opus) — Debian 12, Python

Компоненты:
- Сервер FastAPI: прием ingest (HTTP POST chunked) и отдача live аудио (audio/ogg) слушателям, веб-страница /listen/{id}, API.
- Клиент-стример (Tkinter): выбор устройства, старт/стоп, статус, ссылка и QR-код.
- Формат: Ogg Opus (низкая задержка, хорошее качество).

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

## Как это работает

- При “Старт” клиент вызывает POST /api/start. Сервер создает stream_id и ingest ключ, формирует уникальную ссылку /listen/{id}, а также QR-код.
- Клиент запускает ffmpeg, который читает PCM из stdin и кодирует в Ogg Opus (96 kbps, lowdelay, 20 ms), затем POST’ит поток на /ingest/{id}?key=...
- Сервер одновременно:
  - кеширует заголовки Opus (OpusHead/OpusTags) и небольшой преролл,
  - транслирует входящие чанки всем слушателям.
- Слушатель открывает /listen/{id}, жмет “Начать прослушивание”, браузер воспроизводит /stream/{id} с Content-Type: audio/ogg.

## Советы по задержке и качеству

- Уменьшайте битрейт (-b:a 64k) для экономии канала или увеличивайте до 128k для музыки.
- Блок 20 мс (frame_duration 20) — хороший баланс между задержкой и устойчивостью.
- Если аудио щелкает: увеличьте BLOCKSIZE в streamer.py до 1920 (40 мс).

## Тестирование локально

- Запустите сервер (uvicorn).
- Запустите клиент, укажите http://127.0.0.1:8000 и стартуйте стрим.
- Откройте в браузере ссылку из клиента — слушайте.

