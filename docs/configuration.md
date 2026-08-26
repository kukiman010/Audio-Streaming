# Конфигурация

## Файлы

| Файл | Назначение |
|------|------------|
| `livekit.env` | Секреты и URL для Python-компонентов (не коммитить) |
| `livekit.env.example` | Шаблон переменных окружения |
| `deploy/livekit/livekit.yaml` | Конфиг LiveKit Server (ключи, порты, TURN) |
| `deploy/livekit/turnserver.conf` | Конфиг coturn |
| `deploy/livekit/docker-compose.yml` | Оркестрация контейнеров |

Первичная настройка:

```bash
python setup_livekit.py
```

Скрипт генерирует секреты, записывает их в `livekit.env` и синхронизирует `LIVEKIT_API_SECRET` в `livekit.yaml`.

## Переменные `livekit.env`

| Переменная | Обязательная | Описание |
|------------|--------------|----------|
| `LIVEKIT_API_KEY` | да | Имя API-ключа LiveKit |
| `LIVEKIT_API_SECRET` | да | Секрет для подписи JWT (32+ байт в production) |
| `LIVEKIT_PAIRING_SECRET` | да | Секрет для `/livekit/publisher_token` |
| `LIVEKIT_URL` | да | WebSocket URL LiveKit для клиентов (`ws://` или `wss://`) |
| `HELPER_URL` | рекомендуется | Публичный HTTP URL helper для удалённых клиентов |
| `HELPER_HOST` | нет | Bind-адрес helper (по умолчанию `0.0.0.0`) |
| `HELPER_PORT` | нет | Порт helper (по умолчанию `8000`) |
| `LIVEKIT_DEFAULT_ROOM` | нет | Комната по умолчанию (по умолчанию `audio-room`) |
| `LIVEKIT_PUBLISHER_IDENTITY` | нет | Identity publisher в GUI (по умолчанию `publisher-local`) |
| `ENABLE_LEGACY_TRANSPORT` | нет | `1` — включить устаревший ffmpeg/MP3 транспорт |

`gui_client.py` и `server.py` автоматически загружают `livekit.env` и `.env` при старте.

## Синхронизация ключей

`LIVEKIT_API_KEY` и `LIVEKIT_API_SECRET` в `livekit.env` **должны совпадать** с секцией `keys:` в `deploy/livekit/livekit.yaml`:

```yaml
keys:
  devkey: your_long_secret_here
```

LiveKit читает `livekit.yaml` только при старте контейнера. После правки ключей:

```bash
docker compose -f deploy/livekit/docker-compose.yml restart livekit
```

`start_server.sh` делает restart автоматически. Без перезапуска helper выдаёт JWT по новому секрету, а LiveKit проверяет по старому — результат HTTP 401.

При запуске `server.py` выводит проверку совпадения ключей в stderr.

## Порты и firewall

| Порт | Протокол | Сервис | Назначение |
|------|----------|--------|------------|
| 8000 | TCP | Helper | HTTP API, web-viewer |
| 7880 | TCP | LiveKit | Signaling WebSocket |
| 7881 | TCP | LiveKit | RTC over TCP (fallback) |
| 50000–60000 | UDP | LiveKit | WebRTC media |
| 3478 | UDP/TCP | coturn | STUN/TURN |
| 5349 | TCP | coturn | TURN over TLS |

coturn работает в `network_mode: host` — убедитесь, что порты не заняты другими сервисами.

## Production

### Публичные URL

Для клиентов вне локальной сети задайте реальный IP или домен:

```env
LIVEKIT_URL=ws://stream.example.com:7880
HELPER_URL=http://stream.example.com:8000
```

Не используйте `127.0.0.1` в URL, если клиент на другой машине.

### TLS / WSS

- Браузерный viewer по HTTPS требует `wss://` для LiveKit — иначе mixed content блокируется.
- Настройте TLS на reverse proxy перед LiveKit или используйте `wss://` напрямую.
- В `livekit.yaml` укажите `cert_file` / `key_file` для встроенного TURN TLS.
- В `turnserver.conf` настройте сертификаты для coturn.

### Секреты

- `LIVEKIT_API_SECRET` — минимум 32 байта (`python -c "import secrets; print(secrets.token_urlsafe(36))"`).
- `LIVEKIT_PAIRING_SECRET` — отдельный случайный секрет, не совпадающий с API secret.
- Не коммитьте `livekit.env` в git.

### Внешний IP

В `livekit.yaml`:

```yaml
rtc:
  use_external_ip: true
```

Убедитесь, что UDP 50000–60000 проброшены через NAT/firewall.

После смены диапазона портов или тега образа перезапустите стек и синхронизируйте рабочий `livekit.yaml` / `turnserver.conf` с `*.example` (примерники в репозитории уже на `50000–60000` и `livekit/livekit-server:v1.9`).

## Low-latency publish notes

Publisher (`livekit_client.py`) targets realtime music/game audio:

- `AUDIO_SOURCE_QUEUE_MS = 100` — small LiveKit buffer (not the SDK default 1000 ms).
- Soft SDK reconnect keeps pumping frames (no pause → no drop-oldest burst).
- Hard recovery still pauses while the room/track is torn down and republished.
- Sync queue stays drop-oldest (~200 ms) so lag does not grow under load.

SFU deploy: UDP media **50000–60000**, image **`livekit/livekit-server:v1.9`** (pinned). After pulling example changes, copy them into your local `livekit.yaml` / `turnserver.conf` and recreate the compose stack.

## Troubleshooting

### HTTP 401 от LiveKit («invalid token» / «no permissions»)

1. Проверьте совпадение `livekit.env` ↔ `livekit.yaml` (см. вывод `server.py` при старте).
2. Перезапустите контейнер LiveKit после смены ключей.
3. Смотрите логи: `docker compose -f deploy/livekit/docker-compose.yml logs -f livekit`.
4. Синхронизируйте время (NTP) — JWT зависит от `exp` / `nbf`.

### HTTP 403 на `/livekit/publisher_token`

`pairing_secret` в запросе не совпадает с `LIVEKIT_PAIRING_SECRET` в `livekit.env`.

### Клиент не слышит звук

- Publisher и subscriber в одной room с разными identity.
- Publisher действительно публикует (статус в GUI).
- На Android проверьте, что не включена пауза и выбран правильный аудиовыход.

### Android не подключается

- Порты 7880 и 8000 доступны с устройства.
- В приложении указан публичный IP, не `127.0.0.1`.
- Helper отвечает: `curl http://HOST:8000/healthz`.

## Проверка

```bash
python performance_validation.py
```

Syntax check + smoke test helper endpoints без внешнего LiveKit.
