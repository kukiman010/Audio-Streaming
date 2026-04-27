# Validation Report

## Executed checks

- Syntax validation:
  - `python -m py_compile gui_client.py livekit_client.py audio_devices.py server.py livekit_token.py performance_validation.py`
- Runtime smoke + memory drift probe:
  - `python performance_validation.py`

## Results

- Syntax check: `PASS`
- LiveKit helper service health endpoint: `PASS`
- Token endpoint repeated calls (20 iterations): `PASS`
- Process RSS drift during token test: `28.0 KiB`

## Notes

- The smoke test uses local defaults and does not require running an external LiveKit SFU.
- For production, set a strong `LIVEKIT_API_SECRET` (32+ bytes). The test warning appears because a short dev secret was used.
