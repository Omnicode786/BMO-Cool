# Troubleshooting

Start with the read-only diagnostic:

```bash
sudo -u bmo /opt/bmo_pi/.venv/bin/python /opt/bmo_pi/scripts/diagnose_hardware.py
```

Then inspect logs:

```bash
sudo journalctl -u bmo.service -b --no-pager
sudo journalctl -u bmo.service -f
```

## Service will not start

```bash
sudo systemctl status bmo.service
sudo -u bmo /opt/bmo_pi/.venv/bin/python -c "import bmo; print(bmo.__version__)"
sudo -u bmo /opt/bmo_pi/.venv/bin/python -m pip check
```

Check that `/opt/bmo_pi/.env` exists, is readable by `bmo`, and contains keys without shell quoting mistakes. Never paste keys into logs/issues.

## No microphone

```bash
arecord -l
arecord -L
wpctl status
```

If USB audio vanishes under speaker load, inspect:

```bash
dmesg | tail -100
vcgencmd get_throttled
```

Fix power/cabling before tuning software thresholds.

## Echo canceller not visible

The config should exist at the service user's PipeWire drop-in:

```bash
sudo -u bmo cat /home/bmo/.config/pipewire/pipewire.conf.d/60-bmo-echo-cancel.conf
sudo -u bmo XDG_RUNTIME_DIR=/run/user/$(id -u bmo) wpctl status
```

If the virtual nodes exist but PortAudio naming differs, list sounddevice devices:

```bash
sudo -u bmo /opt/bmo_pi/.venv/bin/python - <<'PY'
import sounddevice as sd
print(sd.query_devices())
PY
```

Update `audio.echo_cancel_source_hint` / `audio.echo_cancel_sink_hint` or explicit microphone/speaker devices.

If PipeWire/WebRTC AEC is not available on your OS image, set `audio.aec_mode: fallback`. Then reduce speaker volume and improve physical separation.

## Self-trigger / barge-in does not work

Self-triggering means microphone audio still resembles the speaker too strongly. Check AEC routing first. Then tune:

- `local_vad_threshold`
- `vad_threshold_while_speaking_multiplier`
- `feedback_suppression_rms`
- `speaker_duck_gain` (fallback mode)

If genuine barge-in is missed, lower the speaking multiplier before lowering the base threshold. Test at real enclosure volume.

## Camera not found

```bash
rpicam-hello --list-cameras
```

Power off before reseating CSI. Check cable orientation and camera connector latch. Current Raspberry Pi OS uses libcamera/Picamera2; do not install legacy `picamera` tutorials into the venv.

Voice remains usable if camera startup fails.

## Vision lag / high CPU

```bash
/opt/bmo_pi/.venv/bin/python /opt/bmo_pi/scripts/benchmark_vision.py
```

Then choose:

```dotenv
BMO_PERFORMANCE_MODE=lite
```

or reduce `performance.normal.camera_fps`, resolution, or increase `vision_every_n_motion_frames`. Local vision drops stale work intentionally; do not “fix” this by making camera queues unbounded.

## Wave never fires

Use a full left/right wave visible to the camera and keep the wrist raised. Tune in small steps:

- decrease `vision.wave.min_amplitude` if the camera field of view makes hand travel small;
- decrease `min_reversals` only if a single reversal is truly desired;
- decrease `min_tracking_confidence` cautiously;
- check `window_sec` against your natural wave speed.

Do not lower `local_confidence_threshold` merely to make demos easier without testing false positives; sub-threshold meaningful candidates already have one-frame cloud escalation.

## Too many wave reactions

Increase `vision.wave.cooldown_sec` (default 20 s), `event_policy.dedupe_window_sec`, or `visual_reaction_cooldown_sec`. The agent is also instructed that silence is acceptable.

## OLED blank

```bash
i2cdetect -y 1
```

If the display appears at `3d` instead of `3c`, update `config/hardware.yaml`. Verify voltage requirements. OLED failure is optional and should not stop voice operation.

## Controls do not respond

Verify BCM pin numbers, ground, and active-low/high wiring in `config/hardware.yaml`. GPIO Zero owns all pins. The reference buttons/joystick use pull-ups and switch to ground.

Use `--dev --keyboard-controls --oled-console` on a desktop to test the control/state/game logic without GPIO.

## Internet/API failures

Check DNS/TLS reachability, then credentials. Diagnostic output only says configured/missing; it does not print keys.

Cloud clients use bounded reconnect/backoff. During internet loss local controls/camera/game/OLED continue and the state can show offline. A persistent provider authentication error will keep failing until the key/model/account issue is corrected.

## ElevenLabs connects but no sound

Verify `ELEVENLABS_VOICE_ID`, output format `pcm_24000`, speaker device/routing, and whether PipeWire BMO Echo Cancel Sink is the intended output. Test hardware separately with `aplay`.

## Brownout/throttling

`vcgencmd get_throttled` is a bitfield. A nonzero historical undervoltage flag is evidence the issue happened even if voltage is currently okay. Fix the supply/cable/peripheral power first; software cannot make an unstable 5 V rail reliable.
