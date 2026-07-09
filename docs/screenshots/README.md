# Скриншоты

Папки для скриншотов интерфейсов проекта. Добавьте изображения и при необходимости вставьте ссылки в корневой [README.md](../../README.md).

## Структура

| Папка | Что снимать |
|-------|-------------|
| `client/` | Desktop publisher — `gui_client.py` (главное окно, выбор устройства, статус стрима) |
| `web-viewer/` | Браузерный viewer — страница `/` на helper-сервере |
| `android/` | Мобильное приложение (главный экран, QR-сканер, воспроизведение в фоне) |

## Рекомендуемые имена файлов

### `client/`

- `main-window.png` — главное окно с подключением
- `device-selection.png` — выбор входного устройства
- `streaming.png` — активный стрим

### `web-viewer/`

- `viewer-main.png` — страница подключения
- `viewer-connected.png` — воспроизведение

### `android/`

- `main-screen.png` — главный экран с IP и кнопками
- `qr-scan.png` — сканирование QR
- `playback.png` — воспроизведение с waveform

## Формат

- PNG или WebP
- Разумное разрешение (до ~1920 px по ширине)
- Без персональных данных и реальных секретов на скриншотах

## Вставка в README

После добавления файлов можно вставить в README:

```markdown
![Desktop client](docs/screenshots/client/main-window.png)
![Web viewer](docs/screenshots/web-viewer/viewer-main.png)
![Android app](docs/screenshots/android/main-screen.png)
```
