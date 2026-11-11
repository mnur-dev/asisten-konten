"""YouTube thumbnail from one chosen broadcast frame.

Two halves, deliberately split:

* the picture is cleaned up by an image model (logos gone, the broadcast's digital
  board gone, background blurred, the two players pushed forward) -- work no filter
  can do, because it has to invent what was behind the thing it removed;
* the headline text is drawn here, in PIL.

Keeping the text local is what makes the feature cheap to use. Every word change
would otherwise be a fresh paid call whose output also redraws the faces slightly,
so a title could never be tweaked without the picture drifting. Drawn locally it is
free, instant, pixel-identical between attempts, and never misspelled -- text is the
one thing image models still get wrong.
"""
import base64
import logging
import os
import subprocess
from pathlib import Path

import requests
from PIL import Image, ImageDraw

from core.render import find_caption_font, fit_wrapped_font

log = logging.getLogger(__name__)

# YouTube's own thumbnail size. compose() resizes to exactly this, so a model that
# only emits its own native sizes still lands here -- which is why the request asks
# for an aspect ratio rather than a pixel size: every model understands 16:9, not
# every model accepts 1280x720.
THUMB_SIZE = (1280, 720)

# 9router's Codex image endpoint takes the reference in `image`, not input_references.
DEFAULT_MODEL = "cx/gpt-6.1-terra"
ROUTER_URL = "https://ep.sukaweb.my.id/v1/images/generations"
ROUTER_MODEL = {"id": DEFAULT_MODEL, "name": "GPT 6.1 Terra (9router)", "usd": None,
                "exact": False, "qualities": None, "aspects": None}
DEFAULT_QUALITY = "high"
DEFAULT_ASPECT = "16:9"
TIMEOUT = 300           # image edits routinely take a minute or two

# The user's own prompt, in their own words (replaced 2026-09-22). Indonesian on
# purpose: it is data sent to the model, not code. {white}/{black} come from the PGN
# headers; {teks} is the thumbnail text -- typed, or picked from Claude's suggestions
# that complement the chosen video title. By the user's choice the model now paints
# the text and an arrow itself; the local PIL headline (text_layer) is still there
# for anyone who prefers free, re-editable text over a paid regenerate per word.
DEFAULT_PROMPT = (
    "edit gambar ini untuk thumbnail video youtube. hilangkan logo2, fokus pada 2 orang "
    "paling depan di layar ({white} di sebelah kiri, {black} di sebelah kanan), sedikit "
    "blur latarnya untuk menambah fokus ke karakter utama, perbesar muka kedua pemain, beri "
    "fokus lebih ke ekspresi pemain kanan. papan catur kayu dan bidak di atasnya biarkan "
    "persis seperti aslinya. tambahkan text \"{teks}\" tetap tulis tanda petiknya. arrow "
    "mengarah ke pemain kanan. text dan arrow warna merah outline warna putih"
)
# Until a thumbnail text is chosen (typed, or picked from Claude's suggestions based on
# the video title), the prompt carries this so it is obvious what still needs filling.
TEXT_PLACEHOLDER = "TULIS TEKS THUMBNAIL"

# The headline look from the user's own thumbnails: bright red, heavy white outline.
TEXT_FILL = "#ff1417"
TEXT_STROKE = "#ffffff"


def find_api_key() -> str | None:
    """Read the shared 9router key on each call, without exposing it in the UI."""
    key_file = Path(os.environ.get("NINEROUTER_KEY_FILE", Path.home() / "pusat-bisnis/pc/9router-key.txt"))
    if os.name == 'nt' and not key_file.is_file():
        key_file = Path('D:/Website/pusat-bisnis/pc/9router-key.txt')
    if key_file.is_file():
        return key_file.read_text(encoding="utf-8-sig").strip() or None
    return None


def grab_frame(video, seconds: float, output) -> Path:
    """The chosen broadcast frame at full resolution, as PNG.

    Full resolution, not the preview size: this is what gets uploaded, and the model
    can only keep detail that was there to begin with. PNG rather than JPEG so the
    faces don't arrive already carrying compression artefacts.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, seconds):.3f}", "-i", str(video),
         "-frames:v", "1", "-f", "image2", "-c:v", "png", str(output)],
        check=True, capture_output=True)
    if not output.is_file() or not output.stat().st_size:
        raise RuntimeError(f"Tidak ada frame di detik {seconds:.2f}")
    return output


def list_models(refresh: bool = False) -> list[dict]:
    """Return the tested 9router model."""
    return [ROUTER_MODEL]


def edit_image(source, prompt: str, output, api_key: str, model: str = DEFAULT_MODEL,
               quality: str | None = DEFAULT_QUALITY, aspect: str = DEFAULT_ASPECT) -> dict:
    """Edit the source frame through 9router; return any reported usage."""
    source, output = Path(source), Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    if model != DEFAULT_MODEL:
        raise ValueError("Model thumbnail harus GPT 6.1 Terra pada 9router")
    body = {"model": model, "prompt": prompt, "image": f"data:image/png;base64,{encoded}"}
    log.info("Mengirim frame ke %s", model)
    response = requests.post(
        ROUTER_URL, headers={"Authorization": f"Bearer {api_key}",
                          "Content-Type": "application/json"},
        json=body, timeout=TIMEOUT)
    if response.status_code != 200:
        # pass the service's own wording through -- an unsupported parameter, an
        # unknown model or an empty balance is only identifiable from its message
        detail = response.text[:400]
        try:
            detail = response.json()["error"]["message"]
        except Exception:
            pass
        raise RuntimeError(f"9router {response.status_code}: {detail}")
    payload = response.json()
    entry = (payload.get("data") or [{}])[0]
    if not (image := entry.get("b64_json")):
        raise RuntimeError("Balasan API tidak memuat gambar")
    raw = base64.b64decode(image, validate=True)
    from io import BytesIO
    with Image.open(BytesIO(raw)) as check:
        check.verify()
    output.write_bytes(raw)
    return payload.get("usage") or {}


def text_layer(size, texts):
    """RGBA overlay with each headline drawn into its own box.

    Same auto-fit machinery the short captions use, so a box drawn in the UI fills
    the way it does there; only the palette differs (red on white rather than the
    short's yellow on black).
    """
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font_path = find_caption_font()
    for item in texts or []:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        x, y, w, h = item["rect"]
        pad_w, pad_h = w * 0.84, h * 0.84          # room for the heavy stroke
        scale = item.get("scale") or 1.0
        font, lines = fit_wrapped_font(draw, text, pad_w, pad_h, font_path, scale=scale)
        line_height = (draw.textbbox((0, 0), "Ag", font=font)[3]) * 1.05
        stroke = max(3, round(font.size * 0.12))
        top = y + h / 2 - line_height * len(lines) / 2
        for index, line in enumerate(lines):
            draw.text((x + w / 2, top + line_height * (index + 0.5)), line, font=font,
                      anchor="mm", fill=item.get("fill") or TEXT_FILL,
                      stroke_width=stroke, stroke_fill=item.get("stroke") or TEXT_STROKE)
    return layer


def compose(base, texts, output) -> Path:
    """The finished thumbnail: the model's picture with the headline drawn on top.

    Always resized to THUMB_SIZE -- a custom size can come back slightly off, and a
    thumbnail that isn't exactly 1280x720 gets rescaled by YouTube instead.
    """
    output = Path(output)
    picture = Image.open(base).convert("RGB")
    if picture.size != THUMB_SIZE:
        picture = picture.resize(THUMB_SIZE, Image.LANCZOS)
    picture = picture.convert("RGBA")
    picture.alpha_composite(text_layer(THUMB_SIZE, texts))
    picture.convert("RGB").save(output, "PNG")
    return output
