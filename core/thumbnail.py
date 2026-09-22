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
import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
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

# OpenRouter: one key, one request shape, every image model behind it. Chosen over
# talking to OpenAI and Google separately because the job here is to find the
# cheapest model that can actually do this edit -- and that is a question you answer
# by trying six of them, not by picking one up front. Rates are passed through
# unchanged (verified against both first-party pricing pages), so the choice costs
# nothing; what it buys is that swapping models is a dropdown, not a rewrite.
API_URL = "https://openrouter.ai/api/v1/images"
MODELS_URL = "https://openrouter.ai/api/v1/images/models"
DEFAULT_MODEL = "openai/gpt-image-2.5-flare"
DEFAULT_QUALITY = "high"
DEFAULT_ASPECT = "16:9"
TIMEOUT = 300           # image edits routinely take a minute or two

# Where the cached model list lives, and how long before it is refetched. Pricing
# needs one request per model, so it is not something to do on every panel open.
MODEL_CACHE = "openrouter-models.json"
CACHE_DAYS = 7

# A 1K image costs about this many output tokens -- derived from Google's own
# published per-image prices against their per-token rate (0.067 / 0.00006). Only
# used to turn token-billed models into a comparable per-edit figure for the
# dropdown; the real number always comes back in usage.cost afterwards.
TOKENS_1K = 1117

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


def env_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def find_api_key() -> str | None:
    """OPENROUTER_API_KEY from the environment, else from a .env file at the repo root.

    .env is gitignored, so a key left there stays out of the repository. Read on
    every call rather than cached at import time so a key pasted into the UI takes
    effect immediately -- this app has no auto-reload, and telling the user to
    restart the server to pick up the key they just saved is a bad first impression.
    """
    if key := os.environ.get("OPENROUTER_API_KEY", "").strip():
        return key
    # On the VPS the key lives next to cookies.txt, outside public_html. It may be
    # saved from Windows Notepad, hence utf-8-sig (BOM) and strip() (CRLF).
    if (key_file := os.environ.get("OPENROUTER_KEY_FILE")) and Path(key_file).is_file():
        if key := Path(key_file).read_text(encoding="utf-8-sig", errors="replace").strip():
            return key
    env = env_path()
    if not env.is_file():
        return None
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "OPENROUTER_API_KEY":
            return value.strip().strip("\"").strip("'") or None
    return None


def save_api_key(key: str) -> None:
    """Store the key in .env, replacing any line already there. Plain text on a
    local-only machine -- the same place the user would have put it by hand."""
    env = env_path()
    lines = []
    if env.is_file():
        lines = [line for line in env.read_text(encoding="utf-8", errors="replace").splitlines()
                 if line.partition("=")[0].strip() != "OPENROUTER_API_KEY"]
    lines.append(f"OPENROUTER_API_KEY={key.strip()}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def _price_per_edit(pricing: list) -> tuple[float | None, bool]:
    """(USD for one edit at THUMB_SIZE, whether that figure is exact).

    OpenRouter states a unit per billable item. "image" and "megapixel" are exact
    for a fixed output size; "token" is not -- on the OpenAI models the token count
    swings about 11x between quality low and max -- so those are marked approximate
    rather than quietly presented as fact.
    """
    megapixels = THUMB_SIZE[0] * THUMB_SIZE[1] / 1_000_000
    total, exact = 0.0, True
    seen = False
    for item in pricing or []:
        if item.get("billable") not in ("output_image", "input_image"):
            continue
        cost, unit = item.get("cost_usd"), item.get("unit")
        if cost is None:
            continue
        if unit == "image":
            total += cost
        elif unit == "megapixel":
            total += cost * megapixels
        elif unit == "token":
            total += cost * TOKENS_1K
            exact = False
        else:
            continue
        seen = seen or item.get("billable") == "output_image"
    return (total if seen else None), exact


def list_models(refresh: bool = False) -> list[dict]:
    """Every image model OpenRouter offers that can take an input image, with a
    per-edit price and the parameters it accepts.

    Cached on disk: the price lives on a per-model endpoint record, so a full list
    is one request plus one per model. Refetched only when the cache is missing,
    stale, or the caller asks -- new models then appear on their own without anyone
    touching this file, which is the point of going through OpenRouter at all.
    """
    cache = env_path().parent / MODEL_CACHE
    if not refresh and cache.is_file():
        age = time.time() - cache.stat().st_mtime
        if age < CACHE_DAYS * 86400:
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except ValueError:
                pass                      # corrupt cache is not worth failing over

    log.info("Mengambil daftar model gambar dari OpenRouter")
    listing = requests.get(MODELS_URL, timeout=60).json()
    entries = listing.get("data", listing)

    def describe(entry: dict) -> dict | None:
        mid = entry.get("id")
        modes = (entry.get("architecture") or {}).get("input_modalities") or []
        if not mid or "image" not in modes:
            return None                   # can't edit what it can't look at
        params = entry.get("supported_parameters") or {}
        quality = params.get("quality") or {}
        try:
            record = requests.get(f"{MODELS_URL}/{mid}/endpoints", timeout=30).json()
            record = record.get("data", record)
            endpoints = record.get("endpoints") or []
            price, exact = _price_per_edit((endpoints[0] if endpoints else {}).get("pricing"))
        except Exception as error:        # one unreachable model must not lose the list
            log.warning("Harga %s tidak terbaca: %s", mid, error)
            price, exact = None, False
        return {"id": mid, "name": entry.get("name") or mid, "usd": price, "exact": exact,
                "qualities": quality.get("values") if isinstance(quality, dict) else None,
                "aspects": (params.get("aspect_ratio") or {}).get("values")
                           if isinstance(params.get("aspect_ratio"), dict) else None}

    with ThreadPoolExecutor(max_workers=8) as pool:
        models = [m for m in pool.map(describe, entries) if m]
    models.sort(key=lambda m: (m["usd"] is None, m["usd"] or 0))
    cache.write_text(json.dumps(models, indent=1), encoding="utf-8")
    log.info("%d model gambar tersimpan di %s", len(models), cache.name)
    return models


def edit_image(source, prompt: str, output, api_key: str, model: str = DEFAULT_MODEL,
               quality: str | None = DEFAULT_QUALITY, aspect: str = DEFAULT_ASPECT) -> dict:
    """One paid call: `source` + `prompt` -> `output`. Returns OpenRouter's usage
    block, which carries `cost` in real dollars for this exact call -- so the project
    records what was actually billed instead of an estimate.

    `quality` is dropped when None: not every model has the parameter, and sending
    one that a model does not accept is a rejected request rather than an ignored
    field.
    """
    source, output = Path(source), Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    body = {
        "model": model, "prompt": prompt, "n": 1,
        "aspect_ratio": aspect, "output_format": "png",
        "input_references": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        ],
    }
    if quality:
        body["quality"] = quality
    log.info("Mengirim frame ke %s (%s%s)", model, aspect,
             f", kualitas {quality}" if quality else "")
    response = requests.post(
        API_URL, headers={"Authorization": f"Bearer {api_key}",
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
        raise RuntimeError(f"OpenRouter {response.status_code}: {detail}")
    payload = response.json()
    entry = (payload.get("data") or [{}])[0]
    if not (image := entry.get("b64_json")):
        raise RuntimeError("Balasan API tidak memuat gambar")
    output.write_bytes(base64.b64decode(image))
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
