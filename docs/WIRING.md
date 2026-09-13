# Wiring and enclosure guide

## Electrical safety first

Raspberry Pi GPIO uses **3.3 V logic and is not 5 V tolerant**. Never connect a 5 V logic output directly to a GPIO input unless a proper level shifter/divider is used and appropriate for the signal. Check each breakout board's real schematic/datasheet; labels such as “5 V compatible” can refer only to its power input.

A passive speaker is a power load, not a GPIO device. Use a suitable amplifier board or powered/USB audio device. Do not attempt to drive speaker current from a GPIO pin.

## Reference pin map

`config/hardware.yaml` is the single source of truth for pins. Default BCM mapping:

```text
OLED SSD1306 I2C
  VCC -> 3.3V (only if supported by your module)
  GND -> GND
  SDA -> GPIO2 / physical pin 3
  SCL -> GPIO3 / physical pin 5

TTP223-style digital touch
  VCC -> 3.3V
  GND -> GND
  OUT -> GPIO17 / physical pin 11

Button A (active-low, internal pull-up)
  one side -> GPIO22 / physical pin 15
  other    -> GND

Button B
  one side -> GPIO23 / physical pin 16
  other    -> GND

Five-way joystick (active-low switches)
  UP    -> GPIO5
  DOWN  -> GPIO6
  LEFT  -> GPIO13
  RIGHT -> GPIO19
  PRESS -> GPIO26
  COM/GND -> GND
```

Do not infer physical header pin numbers from BCM numbers. Use the official Raspberry Pi pinout/documentation when changing pins.

## Camera

Connect the official/compatible camera to the CSI connector with power removed. The cable contact orientation depends on Pi/camera connector generation; follow the camera's official installation guide rather than guessing from a photo.

## Microphone

A USB microphone is the easiest baseline and avoids assuming an analog mic input that the Pi does not have. I2S microphones are possible but require their own overlay/driver wiring and are intentionally not hardcoded into this project.

## Speaker

For the easiest build use a powered USB speaker, USB audio dongle + powered speaker, or another powered audio endpoint. If using a class-D amplifier board:

- size its power supply for peak current;
- share ground where the audio/interface requires it;
- do not route speaker current through thin Pi signal wiring;
- add decoupling per the amplifier vendor;
- keep amplifier switching nodes away from microphone wiring.

## Acoustic layout

For usable barge-in/AEC:

- maximize physical microphone/speaker distance inside the enclosure;
- point the speaker away from the microphone;
- put absorbent/mechanical isolation between them where practical;
- isolate the mic from enclosure panel vibration;
- avoid mounting the microphone inside the speaker's pressure cavity;
- avoid automatic speaker volume so high that the microphone clips.

AEC cannot recover speech reliably from a clipped microphone signal.

## Power

Raspberry Pi 4 baseline recommendation is a good regulated 5 V / 3 A supply. USB cameras, microphones, storage, displays, and amplifiers reduce headroom. Speaker bass peaks are a common way to expose a marginal supply.

Symptoms of power trouble include:

- `vcgencmd get_throttled` undervoltage bits;
- USB microphone/camera reconnects;
- audio pops when volume increases;
- camera failures under load;
- spontaneous reboots.

Power a demanding amplifier separately when appropriate, but preserve the grounding arrangement required by the audio interface. Do not assume Pi 3.3 V/5 V header rails can safely provide arbitrary peripheral current.

Official GPIO reference: <https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio>
