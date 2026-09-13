# Physical controls and OLED expression manual

## Modes

Button B cycles:

1. `companion` — normal voice/vision personality.
2. `game` — offline/local Star Catcher game.
3. `status` — OLED CPU/RAM/temp/camera FPS/vision and queue metrics.
4. `sleep` — quiet/sleeping state; local wake controls remain available.

Joystick press jumps from companion mode into game mode.

## Touch

- single tap: attention reaction; wakes the device if sleeping;
- double tap while speaking: hard interruption/barge-in;
- long press: toggle sleep/wake;
- in `touch_to_talk`, physical press/release also supply manual speech-activity boundaries to Gemini Live.

## Buttons and joystick

- Button A: curious face in companion mode; retry after game-over.
- Button B: cycle mode.
- Joystick left/right: move the catcher in Star Catcher.
- Joystick up/down are reserved for future local UI screens and still emit typed control events.

## Star Catcher

A star falls from the top of the 128x64 OLED. Move the paddle left/right to catch it. Catching increases score; misses consume lives. The game is fully local and never calls Gemini/ElevenLabs.

## Face/state mapping

Immediate state faces:

- BOOTING -> booting/progress face
- IDLE -> idle face using current mood
- LISTENING -> listening face
- THINKING -> thinking dots
- SPEAKING -> animated mouth
- SLEEPING -> sleepy / zZ
- OFFLINE -> offline crossed eyes
- ERROR -> error face

Temporary expressions include `happy`, `curious`, `annoyed`, `angry`, `surprised`, and `proud`. The local mood engine can alter emotional flavor based on recent interaction, then decays toward the configured default. The renderer is original pixel art and intentionally does not reproduce copyrighted character artwork.
