"""Tiny original pixel-face renderer for expressive OLED states and the mini-game."""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from ..events import GameStateUpdated, HealthSnapshot


@dataclass(slots=True)
class FaceFrame:
    expression: str
    blink: bool = False
    mouth_phase: int = 0
    mood: str = "happy"


class FaceRenderer:
    """Render cute game-console-like faces without copying copyrighted character art."""

    def __init__(self, width: int = 128, height: int = 64) -> None:
        self.width = width
        self.height = height
        self.font = ImageFont.load_default()

    def render_face(self, frame: FaceFrame) -> Image.Image:
        image = Image.new("1", (self.width, self.height), 0)
        draw = ImageDraw.Draw(image)
        expression = frame.expression.lower()
        eye_y = 21
        left_x, right_x = int(self.width * 0.34), int(self.width * 0.66)

        if frame.blink or expression == "sleepy":
            draw.line((left_x - 6, eye_y, left_x + 6, eye_y), fill=1, width=2)
            draw.line((right_x - 6, eye_y, right_x + 6, eye_y), fill=1, width=2)
        elif expression in {"angry", "annoyed"}:
            draw.line((left_x - 6, eye_y - 4, left_x + 6, eye_y + 2), fill=1, width=2)
            draw.line((right_x - 6, eye_y + 2, right_x + 6, eye_y - 4), fill=1, width=2)
            draw.ellipse((left_x - 2, eye_y, left_x + 2, eye_y + 4), fill=1)
            draw.ellipse((right_x - 2, eye_y, right_x + 2, eye_y + 4), fill=1)
        elif expression in {"surprised", "error"}:
            draw.ellipse((left_x - 6, eye_y - 6, left_x + 6, eye_y + 6), outline=1, width=2)
            draw.ellipse((right_x - 6, eye_y - 6, right_x + 6, eye_y + 6), outline=1, width=2)
        elif expression == "offline":
            for x in (left_x, right_x):
                draw.line((x - 5, eye_y - 5, x + 5, eye_y + 5), fill=1, width=2)
                draw.line((x + 5, eye_y - 5, x - 5, eye_y + 5), fill=1, width=2)
        elif expression == "curious":
            draw.ellipse((left_x - 5, eye_y - 5, left_x + 5, eye_y + 5), fill=1)
            draw.ellipse((right_x - 7, eye_y - 7, right_x + 7, eye_y + 7), outline=1, width=2)
            draw.ellipse((right_x - 2, eye_y - 2, right_x + 2, eye_y + 2), fill=1)
        else:
            draw.rounded_rectangle((left_x - 6, eye_y - 6, left_x + 6, eye_y + 6), radius=3, fill=1)
            draw.rounded_rectangle((right_x - 6, eye_y - 6, right_x + 6, eye_y + 6), radius=3, fill=1)

        mouth_y = 45
        if expression in {"happy", "idle", "proud"}:
            draw.arc((self.width // 2 - 15, mouth_y - 9, self.width // 2 + 15, mouth_y + 8), 15, 165, fill=1, width=2)
        elif expression in {"angry", "annoyed"}:
            draw.arc((self.width // 2 - 13, mouth_y - 1, self.width // 2 + 13, mouth_y + 14), 195, 345, fill=1, width=2)
        elif expression == "surprised":
            draw.ellipse((self.width // 2 - 5, mouth_y - 5, self.width // 2 + 5, mouth_y + 6), outline=1, width=2)
        elif expression == "speaking":
            height = 4 + (frame.mouth_phase % 3) * 3
            draw.ellipse((self.width // 2 - 9, mouth_y - height // 2, self.width // 2 + 9, mouth_y + height // 2), outline=1, width=2)
        elif expression == "listening":
            draw.line((self.width // 2 - 9, mouth_y, self.width // 2 + 9, mouth_y), fill=1, width=2)
            draw.arc((4, 4, 17, 17), 200, 520, fill=1)
        elif expression == "thinking":
            dots = 1 + (frame.mouth_phase % 3)
            for index in range(dots):
                x = self.width // 2 - 7 + index * 7
                draw.ellipse((x, mouth_y, x + 3, mouth_y + 3), fill=1)
        elif expression == "sleepy":
            draw.line((self.width // 2 - 5, mouth_y, self.width // 2 + 5, mouth_y), fill=1)
            draw.text((self.width - 25, 3), "zZ", font=self.font, fill=1)
        elif expression == "booting":
            draw.rectangle((24, 45, self.width - 24, 49), outline=1)
            progress = 10 + (frame.mouth_phase % 8) * 10
            draw.rectangle((26, 47, min(self.width - 26, 26 + progress), 47), fill=1)
        elif expression == "error":
            draw.text((self.width // 2 - 3, mouth_y - 5), "!", font=self.font, fill=1)
        else:
            draw.line((self.width // 2 - 8, mouth_y, self.width // 2 + 8, mouth_y), fill=1)

        if expression == "happy":
            draw.point((left_x - 10, 34), fill=1)
            draw.point((right_x + 10, 34), fill=1)
        image.info["label"] = f"face:{expression}:{frame.mood}"
        return image


    def render_status(self, snapshot: HealthSnapshot | None) -> Image.Image:
        """Render a compact local system-status dashboard for the physical status mode."""
        image = Image.new("1", (self.width, self.height), 0)
        draw = ImageDraw.Draw(image)
        draw.text((2, 1), "BMO PI STATUS", font=self.font, fill=1)
        if snapshot is None:
            draw.text((2, 18), "waiting for health...", font=self.font, fill=1)
        else:
            temp = "--" if snapshot.cpu_temp_c is None else f"{snapshot.cpu_temp_c:.0f}C"
            draw.text((2, 16), f"CPU {snapshot.cpu_percent:3.0f}%  {temp}", font=self.font, fill=1)
            draw.text((2, 28), f"RAM {snapshot.memory_percent:3.0f}%  CAM {snapshot.camera_fps:.1f}", font=self.font, fill=1)
            draw.text((2, 40), f"VIS {snapshot.vision_latency_ms:.0f}ms", font=self.font, fill=1)
            draw.text((2, 52), f"AQ {snapshot.audio_queue_depth}  TQ {snapshot.tts_queue_depth}", font=self.font, fill=1)
        image.info["label"] = "status"
        return image

    def render_game(self, state: GameStateUpdated) -> Image.Image:
        image = Image.new("1", (self.width, self.height), 0)
        draw = ImageDraw.Draw(image)
        draw.text((2, 1), f"STAR {state.score:02d}  <3 {state.lives}", font=self.font, fill=1)
        star_x, star_y = state.star_x, max(12, state.star_y)
        points = []
        for idx in range(10):
            angle = -math.pi / 2 + idx * math.pi / 5
            radius = 5 if idx % 2 == 0 else 2
            points.append((star_x + int(math.cos(angle) * radius), star_y + int(math.sin(angle) * radius)))
        draw.polygon(points, outline=1)
        px = max(8, min(self.width - 8, state.player_x))
        draw.rectangle((px - 8, self.height - 8, px + 8, self.height - 4), fill=1)
        if state.message:
            draw.rectangle((0, 20, self.width, 33), fill=0)
            draw.text((4, 22), state.message[:20], font=self.font, fill=1)
        image.info["label"] = f"game:score={state.score}:lives={state.lives}"
        return image
