import asyncio
import hmac
import io
import logging
import math
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from reportlab.lib.colors import black
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PRINT_TOKEN = os.environ.get("PRINT_WEB_TOKEN", "")
MAX_PDF_BYTES = 20 * 1024 * 1024
QUEUE = os.environ.get("CUPS_QUEUE", "munbyn")

HA_BASE_URL = os.environ.get("HA_BASE_URL", "").strip().rstrip("/")
HA_WAKE_WEBHOOK_ID = os.environ.get("HA_WAKE_WEBHOOK_ID", "").strip()
HA_SLEEP_WEBHOOK_ID = os.environ.get("HA_SLEEP_WEBHOOK_ID", "").strip()
POWER_SETTINGS = (HA_BASE_URL, HA_WAKE_WEBHOOK_ID, HA_SLEEP_WEBHOOK_ID)
if any(POWER_SETTINGS) and not all(POWER_SETTINGS):
    raise RuntimeError("Configure HA_BASE_URL and both Home Assistant webhook IDs, or leave all three empty")

HA_POWER_CONTROL_ENABLED = all(POWER_SETTINGS)
PRINTER_STARTUP_DELAY_SECONDS = float(os.environ.get("PRINTER_STARTUP_DELAY_SECONDS", "15"))
PRINTER_IDLE_TIMEOUT_SECONDS = float(os.environ.get("PRINTER_IDLE_TIMEOUT_SECONDS", "120"))
PRINTER_STATUS_POLL_SECONDS = float(os.environ.get("PRINTER_STATUS_POLL_SECONDS", "5"))
HA_WEBHOOK_TIMEOUT_SECONDS = float(os.environ.get("HA_WEBHOOK_TIMEOUT_SECONDS", "10"))

if PRINTER_STARTUP_DELAY_SECONDS < 0:
    raise RuntimeError("PRINTER_STARTUP_DELAY_SECONDS must be zero or greater")
if PRINTER_IDLE_TIMEOUT_SECONDS < 0:
    raise RuntimeError("PRINTER_IDLE_TIMEOUT_SECONDS must be zero or greater")
if PRINTER_STATUS_POLL_SECONDS <= 0:
    raise RuntimeError("PRINTER_STATUS_POLL_SECONDS must be greater than zero")
if HA_WEBHOOK_TIMEOUT_SECONDS <= 0:
    raise RuntimeError("HA_WEBHOOK_TIMEOUT_SECONDS must be greater than zero")

if HA_POWER_CONTROL_ENABLED:
    ha_url = urlsplit(HA_BASE_URL)
    if ha_url.scheme not in {"http", "https"} or not ha_url.hostname or ha_url.path not in {"", "/"} or ha_url.query or ha_url.fragment or ha_url.username or ha_url.password:
        raise RuntimeError("HA_BASE_URL must be an http(s) Home Assistant base URL without credentials, path, query, or fragment")
    for webhook_id in (HA_WAKE_WEBHOOK_ID, HA_SLEEP_WEBHOOK_ID):
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", webhook_id):
            raise RuntimeError("Home Assistant webhook IDs must be 16–128 letters, digits, underscores, or hyphens")

logger = logging.getLogger("munbyn_power")
printer_power_lock = asyncio.Lock()
printer_power_manager_task = None
printer_is_awake = False
power_off_armed = False
queue_idle_since = None


@asynccontextmanager
async def lifespan(_app):
    global printer_power_manager_task
    if HA_POWER_CONTROL_ENABLED:
        printer_power_manager_task = asyncio.create_task(
            manage_printer_power(), name="munbyn-printer-power-manager"
        )
    try:
        yield
    finally:
        if printer_power_manager_task:
            printer_power_manager_task.cancel()
            try:
                await printer_power_manager_task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="Little Rose Label Printer",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


class AddressLabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    address_line_1: str
    address_line_2: str = ""
    town: str
    county: str = ""
    postcode: str
    instagram: str = "@little.rose_shop"
    facebook: str = "@littlerosegifts"
    website: str = "www.littlerose.shop"
    copies: int = 1

if len(PRINT_TOKEN) < 32:
    raise RuntimeError("PRINT_WEB_TOKEN must be configured with at least 32 characters")

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

PAGE_WIDTH, PAGE_HEIGHT = 288, 432  # 4 x 6 inches, matching the Munbyn queue setting.
STICKER_WIDTH_MM, STICKER_HEIGHT_MM = 57, 32
STICKER_WIDTH, STICKER_HEIGHT = STICKER_WIDTH_MM / 25.4 * 72, STICKER_HEIGHT_MM / 25.4 * 72
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
pdfmetrics.registerFont(TTFont("DejaVu", str(FONT_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(FONT_DIR / "DejaVuSans-Bold.ttf")))


def clean_field(value: str, label: str, *, required: bool = False, limit: int = 80) -> str:
    value = value.strip()
    if required and not value:
        raise HTTPException(status_code=400, detail=f"{label} is required")
    if len(value) > limit:
        raise HTTPException(status_code=400, detail=f"{label} must be {limit} characters or fewer")
    if any(ord(character) < 32 for character in value):
        raise HTTPException(status_code=400, detail=f"{label} contains an invalid character")
    return value


def draw_fitted(canvas_obj, text: str, font: str, size: int, x: int, y: int, max_width: int, *, bold=False):
    font_name = "DejaVu-Bold" if bold else "DejaVu"
    while size > 12 and pdfmetrics.stringWidth(text, font_name, size) > max_width:
        size -= 1
    if pdfmetrics.stringWidth(text, font_name, size) > max_width:
        raise HTTPException(status_code=400, detail="A name or address line is too long to fit on a 4 x 6 label")
    canvas_obj.setFont(font_name, size)
    canvas_obj.drawString(x, y, text)


def create_address_label(address: dict[str, str], brand_details: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT), pageCompression=1)
    pdf.setTitle("Little Rose address label")
    pdf.setAuthor("Little Rose")
    pdf.setFillColor(black)

    # Header and the same monochrome badge used on the thermal test label.
    pdf.setLineWidth(0.65)
    pdf.line(24, PAGE_HEIGHT - 31, PAGE_WIDTH - 24, PAGE_HEIGHT - 31)
    pdf.setLineWidth(0.6)
    pdf.circle(44, PAGE_HEIGHT - 65, 20, stroke=1, fill=0)
    logo = Path(__file__).parent / "static" / "little-rose-badge-mono.png"
    pdf.drawImage(ImageReader(str(logo)), 24, PAGE_HEIGHT - 85, width=40, height=40, mask="auto")
    pdf.setFont("DejaVu-Bold", 15)
    pdf.drawString(74, PAGE_HEIGHT - 60, "LITTLE ROSE")
    pdf.setFont("DejaVu", 8.5)
    pdf.drawString(74, PAGE_HEIGHT - 73, "littlerose.shop")
    pdf.setLineWidth(0.65)
    pdf.line(24, PAGE_HEIGHT - 91, PAGE_WIDTH - 24, PAGE_HEIGHT - 91)

    pdf.setFont("DejaVu-Bold", 9)
    pdf.drawString(24, PAGE_HEIGHT - 111, "SHIP TO")
    lines = [address["address_line_1"], address["address_line_2"], address["town"], address["county"]]
    lines = [line for line in lines if line]
    draw_fitted(pdf, address["name"], "DejaVu-Bold", 22, 24, PAGE_HEIGHT - 151, 240, bold=True)
    y = PAGE_HEIGHT - 178
    for line in lines:
        if y < 124:
            raise HTTPException(status_code=400, detail="The address is too long to fit on a 4 x 6 label")
        draw_fitted(pdf, line, "DejaVu", 19, 24, y, 240)
        y -= 25
    if y < 104:
        raise HTTPException(status_code=400, detail="The address is too long to fit on a 4 x 6 label")
    draw_fitted(pdf, address["postcode"], "DejaVu-Bold", 20, 24, y - 2, 240, bold=True)

    footer_y = 39
    pdf.setLineWidth(0.65)
    pdf.line(24, footer_y + 22, PAGE_WIDTH - 24, footer_y + 22)
    text_y = footer_y + 6

    def globe(x, y):
        pdf.setLineWidth(0.85)
        pdf.circle(x + 5, y + 3, 4.2, stroke=1, fill=0)
        pdf.ellipse(x + 2.8, y - 1.2, x + 7.2, y + 7.2, stroke=1, fill=0)
        pdf.line(x + 1, y + 3, x + 9, y + 3)

    def instagram(x, y):
        pdf.setLineWidth(0.85)
        pdf.roundRect(x + 0.8, y - 1.2, 8.4, 8.4, 2, stroke=1, fill=0)
        pdf.circle(x + 5, y + 3, 2.05, stroke=1, fill=0)
        pdf.circle(x + 7.45, y + 5.45, 0.55, stroke=1, fill=1)

    def facebook(x, y):
        pdf.setLineWidth(0.85)
        pdf.circle(x + 5, y + 3, 4.3, stroke=1, fill=0)
        pdf.setFont("DejaVu-Bold", 8)
        pdf.drawCentredString(x + 5, y + 0.2, "f")

    for x, icon, label in [
        (24, instagram, brand_details["instagram"]),
        (104, facebook, brand_details["facebook"]),
        (184, globe, brand_details["website"]),
    ]:
        if not label:
            continue
        icon(x, text_y)
        font_size = 5.8
        while font_size > 4.2 and pdfmetrics.stringWidth(label, "DejaVu", font_size) > 66:
            font_size -= 0.2
        if pdfmetrics.stringWidth(label, "DejaVu", font_size) > 66:
            raise HTTPException(status_code=400, detail="A Little Rose social or website detail is too long to fit")
        pdf.setFont("DejaVu", font_size)
        pdf.drawString(x + 13, text_y + 0.7, label)

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def build_address_label_pdf(
    name: str,
    address_line_1: str,
    address_line_2: str,
    town: str,
    county: str,
    postcode: str,
    instagram: str,
    facebook: str,
    website: str,
) -> bytes:
    address = {
        "name": clean_field(name, "Recipient name", required=True, limit=60),
        "address_line_1": clean_field(address_line_1, "Address line 1", required=True),
        "address_line_2": clean_field(address_line_2, "Address line 2"),
        "town": clean_field(town, "Town or city", required=True, limit=60),
        "county": clean_field(county, "County"),
        "postcode": re.sub(r"\s+", "", clean_field(postcode, "Postcode", required=True, limit=10).upper()),
    }
    if re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?\d[A-Z]{2}", address["postcode"]):
        address["postcode"] = address["postcode"][:-3] + " " + address["postcode"][-3:]
    brand_details = {
        "instagram": clean_field(instagram, "Instagram handle", limit=36),
        "facebook": clean_field(facebook, "Facebook handle", limit=36),
        "website": clean_field(website, "Website address", limit=50),
    }
    return create_address_label(address, brand_details)


def create_sticker_pdf(
    message: str,
    logo_x_mm: float,
    logo_y_mm: float,
    logo_size_mm: float,
    text_x_mm: float,
    text_y_mm: float,
    text_size_pt: float,
) -> bytes:
    """Create a user-positioned Little Rose logo sticker for 57 x 32 mm stock."""
    message = clean_field(message, "Sticker message", required=True, limit=56)
    if not message.strip():
        raise HTTPException(status_code=400, detail="Sticker message is required")
    if any(not math.isfinite(value) for value in (logo_x_mm, logo_y_mm, logo_size_mm, text_x_mm, text_y_mm, text_size_pt)):
        raise HTTPException(status_code=400, detail="Sticker layout values must be finite numbers")
    if not 0 <= logo_x_mm <= STICKER_WIDTH_MM or not 0 <= text_x_mm <= STICKER_WIDTH_MM:
        raise HTTPException(status_code=400, detail="Sticker elements must stay within the label width")
    if not 0 <= logo_y_mm <= STICKER_HEIGHT_MM or not 0 <= text_y_mm <= STICKER_HEIGHT_MM:
        raise HTTPException(status_code=400, detail="Sticker elements must stay within the label height")
    if not 6 <= logo_size_mm <= 27:
        raise HTTPException(status_code=400, detail="Logo size must be between 6 and 27 mm")
    if not 5 <= text_size_pt <= 14:
        raise HTTPException(status_code=400, detail="Message size must be between 5 and 14 points")

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(STICKER_WIDTH, STICKER_HEIGHT), pageCompression=1)
    pdf.setTitle("Little Rose custom sticker")
    pdf.setAuthor("Little Rose")
    pdf.setFillColor(black)

    logo_size = logo_size_mm / 25.4 * 72
    logo_x = logo_x_mm / 25.4 * 72
    logo_y = STICKER_HEIGHT - logo_y_mm / 25.4 * 72
    if logo_x - logo_size / 2 < 2 / 25.4 * 72 or logo_x + logo_size / 2 > STICKER_WIDTH - 2 / 25.4 * 72:
        raise HTTPException(status_code=400, detail="Move the badge away from the label edge")
    if logo_y - logo_size / 2 < 2 / 25.4 * 72 or logo_y + logo_size / 2 > STICKER_HEIGHT - 2 / 25.4 * 72:
        raise HTTPException(status_code=400, detail="Move the badge away from the label edge")

    logo = Path(__file__).parent / "static" / "little-rose-badge-mono.png"
    pdf.drawImage(
        ImageReader(str(logo)),
        logo_x - logo_size / 2,
        logo_y - logo_size / 2,
        width=logo_size,
        height=logo_size,
        mask="auto",
    )

    text_x = text_x_mm / 25.4 * 72
    text_y = STICKER_HEIGHT - text_y_mm / 25.4 * 72
    max_text_width = 30 / 25.4 * 72
    words = message.split()
    lines = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if line and pdfmetrics.stringWidth(candidate, "DejaVu-Bold", text_size_pt) > max_text_width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    if len(lines) > 3 or any(pdfmetrics.stringWidth(part, "DejaVu-Bold", text_size_pt) > max_text_width for part in lines):
        raise HTTPException(status_code=400, detail="Shorten the message or reduce its size to fit the sticker")

    line_height = text_size_pt * 1.18
    total_height = line_height * len(lines)
    if text_y - total_height / 2 < 2 / 25.4 * 72 or text_y + total_height / 2 > STICKER_HEIGHT - 2 / 25.4 * 72:
        raise HTTPException(status_code=400, detail="Move the message away from the label edge")
    pdf.setFont("DejaVu-Bold", text_size_pt)
    first_baseline = text_y + (len(lines) - 1) * line_height / 2 - text_size_pt * 0.35
    for index, text_line in enumerate(lines):
        line_width = pdfmetrics.stringWidth(text_line, "DejaVu-Bold", text_size_pt)
        if text_x - line_width / 2 < 2 / 25.4 * 72 or text_x + line_width / 2 > STICKER_WIDTH - 2 / 25.4 * 72:
            raise HTTPException(status_code=400, detail="Move the message away from the label edge")
        pdf.drawCentredString(text_x, first_baseline - index * line_height, text_line)

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _post_home_assistant_webhook(webhook_id: str, action: str) -> None:
    request = urllib.request.Request(
        f"{HA_BASE_URL}/api/webhook/{webhook_id}",
        data=b"",
        headers={"Content-Length": "0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HA_WEBHOOK_TIMEOUT_SECONDS) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Home Assistant {action} webhook returned HTTP {response.status}")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Home Assistant {action} webhook returned HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError(f"Could not reach the Home Assistant {action} webhook") from None


async def call_home_assistant_webhook(webhook_id: str, action: str) -> None:
    await asyncio.to_thread(_post_home_assistant_webhook, webhook_id, action)


async def submit_bytes_with_power(data: bytes, copies: int, print_option: str = "PageSize=w288h432"):
    global printer_is_awake, power_off_armed, queue_idle_since

    async with printer_power_lock:
        if HA_POWER_CONTROL_ENABLED:
            if not printer_is_awake:
                try:
                    await call_home_assistant_webhook(HA_WAKE_WEBHOOK_ID, "wake")
                except RuntimeError as error:
                    raise HTTPException(status_code=503, detail=str(error)) from None
                if PRINTER_STARTUP_DELAY_SECONDS > 0:
                    await asyncio.sleep(PRINTER_STARTUP_DELAY_SECONDS)
                printer_is_awake = True

            # Arm shutdown before submitting. If CUPS rejects the job, the printer
            # still returns to its idle/off state after the configured delay.
            power_off_armed = True
            queue_idle_since = None

        return await asyncio.to_thread(submit_bytes, data, copies, print_option)


async def manage_printer_power() -> None:
    global printer_is_awake, power_off_armed, queue_idle_since

    while True:
        await asyncio.sleep(PRINTER_STATUS_POLL_SECONDS)
        if not power_off_armed:
            continue

        try:
            active_jobs = await asyncio.to_thread(read_jobs, "not-completed")
        except Exception:
            logger.warning("Could not read CUPS queue; keeping the printer powered")
            continue

        if active_jobs:
            queue_idle_since = None
            continue

        if queue_idle_since is None:
            queue_idle_since = time.monotonic()
            continue
        if time.monotonic() - queue_idle_since < PRINTER_IDLE_TIMEOUT_SECONDS:
            continue

        async with printer_power_lock:
            if not power_off_armed:
                continue
            try:
                active_jobs = await asyncio.to_thread(read_jobs, "not-completed")
            except Exception:
                logger.warning("Could not recheck CUPS queue; keeping the printer powered")
                continue

            if active_jobs:
                queue_idle_since = None
                continue
            if queue_idle_since is None or time.monotonic() - queue_idle_since < PRINTER_IDLE_TIMEOUT_SECONDS:
                continue

            try:
                await call_home_assistant_webhook(HA_SLEEP_WEBHOOK_ID, "sleep")
            except RuntimeError as error:
                logger.warning("%s; will retry after another idle interval", error)
                queue_idle_since = time.monotonic()
                continue

            power_off_armed = False
            printer_is_awake = False
            queue_idle_since = None
            logger.info("Printer powered off after the CUPS queue stayed idle")


def authorize(authorization: str | None) -> None:
    expected = f"Bearer {PRINT_TOKEN}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid print-service token")


def validate_page_origin(request: Request) -> None:
    """Only accept browser form submissions coming from this same local origin."""
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Cross-origin print requests are not allowed")


@app.get("/health")
def health():
    result = subprocess.run(["lpstat", "-p", QUEUE], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise HTTPException(status_code=503, detail="Printer queue is unavailable")
    return {"status": "ok", "queue": QUEUE}


def read_jobs(which: str):
    result = subprocess.run(
        ["lpstat", "-W", which, "-o", QUEUE],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode:
        raise HTTPException(status_code=503, detail="Could not read the printer queue")
    jobs = []
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) == 4:
            jobs.append({"job_id": parts[0], "owner": parts[1], "size": parts[2], "submitted": parts[3]})
    return jobs


@app.get("/api/status")
def printer_status():
    result = subprocess.run(["lpstat", "-p", QUEUE], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise HTTPException(status_code=503, detail="Could not read printer status")
    printer_line = result.stdout.strip()
    lowered = printer_line.lower()
    enabled = "disabled" not in lowered
    current_match = re.search(r"now printing\s+(\S+)", printer_line, re.IGNORECASE)
    state = "Paused" if not enabled else "Printing" if current_match else "Idle"
    active_jobs = read_jobs("not-completed")
    completed_jobs = read_jobs("completed")
    current_job_id = current_match.group(1) if current_match else None
    jobs = [
        {**job, "state": "Printing" if job["job_id"] == current_job_id else "Queued"}
        for job in active_jobs
    ]
    jobs.extend({**job, "state": "Completed"} for job in reversed(completed_jobs[-5:]))
    return {
        "queue": QUEUE,
        "state": state,
        "enabled": enabled,
        "active_count": len(active_jobs),
        "jobs": jobs,
        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#fbf4e8"><title>Print a label · Little Rose</title>
<style>
:root{color-scheme:light;--cream:#fbf4e8;--paper:#fffefd;--ink:#17151c;--muted:#716c68;--line:#e9e0d3;--rose:#a66d55;--soft:#f6ecdd}
*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:16px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.topbar{height:76px;border-bottom:1px solid rgba(60,43,35,.08);display:flex;align-items:center;justify-content:center;background:rgba(251,244,232,.94)}
.brand{display:flex;align-items:center;gap:10px;color:inherit;text-decoration:none}.brand img{width:42px;height:42px;object-fit:contain;border-radius:50%}.brand span{font-family:Georgia,"Times New Roman",serif;font-size:22px;line-height:.88;font-style:italic;letter-spacing:.01em}.brand span em{display:block;text-align:right;font-size:19px;font-weight:400}
main{width:min(100% - 36px,840px);margin:52px auto 64px}.eyebrow{margin:0 0 8px;color:var(--rose);font-weight:750;font-size:.75rem;letter-spacing:.19em;text-transform:uppercase}
h1,h2{font-family:Georgia,"Times New Roman",serif;letter-spacing:-.035em}h1{font-size:clamp(2.1rem,5vw,3rem);line-height:1.08;margin:0}.intro{margin:12px 0 28px;color:var(--muted);font-size:1.05rem}
.card{background:var(--paper);border:1px solid var(--line);border-radius:26px;padding:clamp(22px,5vw,38px);box-shadow:0 2px 4px rgba(49,35,26,.035)}.card-head{display:flex;align-items:flex-start;gap:16px;margin-bottom:25px}.card-icon{width:48px;height:48px;border-radius:50%;background:var(--soft);display:grid;place-items:center;font-size:22px;flex:0 0 auto}.card-head h2{font-size:1.4rem;margin:0 0 4px}.card-head p{margin:0;color:var(--muted);font-size:.94rem}
label{display:block;margin:19px 0 7px;font-weight:700;font-size:.92rem}input{font:inherit;color:var(--ink)}input[type=file]{width:100%;min-height:58px;padding:10px;border:1px solid #ded8d0;border-radius:13px;background:#fff}input[type=file]::file-selector-button{border:0;border-radius:999px;background:var(--soft);padding:9px 14px;margin-right:12px;color:var(--ink);font:inherit;font-weight:650;cursor:pointer}
.address-grid{display:grid;grid-template-columns:1fr 1fr;column-gap:16px}.field.full{grid-column:1/-1}.text-input{width:100%;height:48px;padding:9px 12px;border:1px solid #ded8d0;border-radius:12px;background:#fff}.text-input:focus,.copies:focus{outline:2px solid #a66d55;outline-offset:1px}
.paste-address{margin:0 0 24px;padding:17px;border:1px solid var(--line);border-radius:16px;background:#fffaf3}.paste-address label{margin:0 0 7px}.paste-address textarea{display:block;width:100%;min-height:112px;padding:11px 12px;border:1px solid #ded8d0;border-radius:12px;background:#fff;color:var(--ink);font:inherit;resize:vertical}.paste-address textarea:focus{outline:2px solid #a66d55;outline-offset:1px}.paste-address .paste-actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:10px}.paste-address .secondary{margin:0;min-height:42px;padding:8px 17px}.paste-feedback{margin:0;color:var(--muted);font-size:.84rem}
.brand-fields{margin-top:16px;padding-top:17px;border-top:1px solid var(--line)}.brand-fields h3{margin:0;font-size:1rem}.brand-fields .hint{margin-top:3px}.actions{display:flex;flex-wrap:wrap;align-items:center;gap:12px}.secondary{margin-top:25px;min-height:52px;padding:12px 22px;border:1px solid #d8cec1;border-radius:999px;background:#fff;color:var(--ink);font:inherit;font-weight:700;cursor:pointer}.secondary:hover{background:var(--soft)}.submit:disabled{opacity:.45;cursor:not-allowed;transform:none}
.copies{width:112px;height:48px;padding:8px 12px;border:1px solid #ded8d0;border-radius:12px;background:#fff}.hint{color:var(--muted);font-size:.84rem;margin:7px 0 0}.submit{margin-top:25px;min-height:52px;padding:12px 24px;border:0;border-radius:999px;background:#050505;color:white;font:inherit;font-weight:750;cursor:pointer;transition:transform .15s,background .15s}.submit:hover{background:#33252d;transform:translateY(-1px)}.submit:disabled{opacity:.6;cursor:wait;transform:none}.submit span{margin-left:9px}
#queue{margin-bottom:20px;padding:22px 25px;border:1px solid var(--line);border-radius:22px;background:rgba(255,254,253,.72)}.queue-top{display:flex;justify-content:space-between;align-items:flex-start;gap:14px}.queue-title{display:flex;align-items:center;gap:12px}.queue-title h2{margin:0;font-size:1.2rem}.queue-title p{margin:3px 0 0;color:var(--muted);font-size:.86rem}.queue-icon{width:40px;height:40px;display:grid;place-items:center;border-radius:50%;background:var(--soft);font-size:20px}.badge{display:inline-flex;align-items:center;gap:7px;padding:6px 11px;border-radius:999px;background:#edf5e9;color:#355b32;font-size:.78rem;font-weight:750;white-space:nowrap}.badge:before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}.badge.printing{background:#fff2d9;color:#875313}.badge.paused,.badge.error{background:#fbe8e5;color:#9b3328}.queue-meta{margin:15px 0 0;color:var(--muted);font-size:.86rem}.job-list{margin-top:12px;border-top:1px solid var(--line)}.job{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line);font-size:.83rem}.job:last-child{border-bottom:0;padding-bottom:0}.job-main{min-width:0}.job-id{font-weight:750}.job-detail{color:var(--muted);overflow-wrap:anywhere}.job-state{color:var(--muted);font-weight:650;white-space:nowrap}.refresh{border:0;background:transparent;color:var(--muted);font:inherit;font-size:.82rem;text-decoration:underline;cursor:pointer;padding:4px 0}.queue-error{color:#9b3328}
#status{min-height:26px;margin:16px 0 0;font-weight:650}#status.error{color:#9b3328}.note{display:flex;gap:10px;align-items:flex-start;margin:20px 3px 0;color:var(--muted);font-size:.86rem}.note b{color:var(--ink)}footer{text-align:center;color:var(--muted);font-size:.83rem;padding:0 20px 30px}footer span{color:#b17c62}
.sticker-canvas{position:relative;width:min(100%,570px);aspect-ratio:57/32;margin:20px auto 24px;overflow:hidden;border:1px solid #b7a17e;border-radius:4px;background:#d8c49f;box-shadow:0 8px 24px rgba(49,35,26,.13);touch-action:none}.sticker-canvas:after{content:"57 × 32 mm";position:absolute;right:8px;bottom:5px;color:rgba(48,38,25,.58);font-size:11px;pointer-events:none}.sticker-item{position:absolute;left:0;top:0;transform:translate(-50%,-50%);cursor:grab;user-select:none;touch-action:none}.sticker-item:active{cursor:grabbing}.sticker-badge{height:auto;object-fit:contain;filter:contrast(1.1)}.sticker-message-preview{max-width:53%;color:#17151c;text-align:center;font-family:Georgia,"Times New Roman",serif;font-weight:700;line-height:1.14;overflow-wrap:anywhere}.sticker-controls{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px}.sticker-control{margin:0}.sticker-control input[type=range]{width:100%;accent-color:var(--rose)}.sticker-control output{color:var(--muted);font-size:.82rem;font-weight:600}.sticker-instructions{margin:0 0 4px;color:var(--muted);font-size:.88rem}.sticker-copy{width:112px;height:48px;padding:8px 12px;border:1px solid #ded8d0;border-radius:12px;background:#fff}.sticker-dialog{width:min(520px,96vw);height:min(620px,92vh)}.sticker-dialog .preview-frame{height:calc(100% - 116px)}
@media(max-width:560px){main{margin:34px auto 46px}.topbar{height:66px}.card{border-radius:20px}.card-head{gap:12px}.card-icon{width:42px;height:42px}#queue{padding:18px;border-radius:18px}.address-grid{grid-template-columns:1fr}.field.full{grid-column:auto}}
@media(max-width:560px){.sticker-controls{grid-template-columns:1fr}.sticker-canvas{margin:16px auto 20px}}
dialog{width:min(900px,96vw);height:min(900px,94vh);max-width:none;max-height:none;padding:0;border:1px solid var(--line);border-radius:20px;background:var(--paper);color:var(--ink);box-shadow:0 24px 80px rgba(24,16,12,.28)}dialog::backdrop{background:rgba(24,16,12,.58)}.preview-head{height:62px;padding:0 18px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line)}.preview-head h2{margin:0;font-size:1.2rem}.preview-close{border:0;background:transparent;font-size:1.6rem;cursor:pointer;color:var(--muted)}.preview-frame{display:block;width:100%;height:calc(100% - 116px);border:0;background:#f3f3f3}.preview-foot{min-height:68px;padding:8px 16px;display:flex;align-items:center;justify-content:space-between;gap:14px;color:var(--muted);font-size:.84rem}.preview-actions{display:flex;align-items:center;gap:8px;flex:0 0 auto}.preview-print{margin:0;min-height:42px;padding:8px 18px}.preview-print:disabled{opacity:.5;cursor:not-allowed}.preview-message{flex:1;margin:0;min-width:0}.preview-message.error{color:#9b3328}
</style></head><body>
<header class="topbar"><a class="brand" href="https://www.littlerose.shop" aria-label="Little Rose"><img src="/static/little-rose-badge.png" alt=""><span>little<em>rose</em></span></a></header>
<main><p class="eyebrow">Little Rose · Dispatch</p><h1>Print Little Rose labels</h1><p class="intro">Design a small branded sticker or send a 4 × 6 inch label to the Munbyn printer.</p>
<section id="queue" aria-labelledby="queue-heading"><div class="queue-top"><div class="queue-title"><div class="queue-icon" aria-hidden="true">▤</div><div><h2 id="queue-heading">Printer queue</h2><p>Munbyn label printer</p></div></div><span id="queue-badge" class="badge">Checking…</span></div><p id="queue-meta" class="queue-meta">Checking printer status…</p><div id="job-list" class="job-list" aria-live="polite"></div><div style="text-align:right;margin-top:8px"><button id="refresh" class="refresh" type="button">Refresh status</button></div></section>
<section class="card" aria-labelledby="address-title"><div class="card-head"><div class="card-icon" aria-hidden="true">✉</div><div><h2 id="address-title">Create a Little Rose label</h2><p>Enter the delivery address and we’ll lay it out on a 4 × 6 label.</p></div></div>
<form id="address-form"><div class="paste-address"><label for="pasted-address">Paste a full address</label><textarea id="pasted-address" autocomplete="off" placeholder="Recipient name&#10;Address line 1&#10;Town or city&#10;County (optional)&#10;Postcode"></textarea><div class="paste-actions"><button class="secondary" id="fill-from-paste" type="button">Fill address fields</button><p class="paste-feedback" id="paste-feedback" role="status" aria-live="polite">One item per line. Check the fields below after filling.</p></div></div><div class="address-grid">
<div class="field full"><label for="recipient-name">Recipient name</label><input class="text-input" id="recipient-name" name="name" maxlength="60" autocomplete="name" required></div>
<div class="field full"><label for="address-line-1">Address line 1</label><input class="text-input" id="address-line-1" name="address_line_1" maxlength="80" autocomplete="address-line1" required></div>
<div class="field full"><label for="address-line-2">Address line 2 <span class="hint">(optional)</span></label><input class="text-input" id="address-line-2" name="address_line_2" maxlength="80" autocomplete="address-line2"></div>
<div class="field"><label for="town">Town or city</label><input class="text-input" id="town" name="town" maxlength="60" autocomplete="address-level2" required></div>
<div class="field"><label for="county">County <span class="hint">(optional)</span></label><input class="text-input" id="county" name="county" maxlength="80" autocomplete="address-level1"></div>
<div class="field"><label for="postcode">Postcode</label><input class="text-input" id="postcode" name="postcode" maxlength="10" autocomplete="postal-code" required></div>
<div class="field"><label for="address-copies">Copies</label><input class="copies" id="address-copies" name="copies" type="number" min="1" max="10" value="1" required></div>
<div class="field full brand-fields"><h3>Little Rose details on the label</h3><p class="hint">These start with your defaults and stay saved in this browser when you change them.</p></div>
<div class="field"><label for="instagram">Instagram handle</label><input class="text-input brand-input" id="instagram" name="instagram" value="@little.rose_shop" maxlength="36" autocomplete="off"></div>
<div class="field"><label for="facebook">Facebook handle</label><input class="text-input brand-input" id="facebook" name="facebook" value="@littlerosegifts" maxlength="36" autocomplete="off"></div>
<div class="field full"><label for="website">Website address</label><input class="text-input brand-input" id="website" name="website" value="www.littlerose.shop" maxlength="50" autocomplete="url"></div>
</div><p class="hint">Preview the label first. From the preview, choose Proceed to print to send it to the Munbyn.</p>
<div class="actions"><button class="submit" id="preview-label" type="submit">Preview label <span aria-hidden="true">→</span></button></div></form><div id="address-status" role="status" aria-live="polite"></div></section>
<section class="card" aria-labelledby="sticker-title" style="margin-top:18px"><div class="card-head"><div class="card-icon" aria-hidden="true">✿</div><div><h2 id="sticker-title">Design a Little Rose sticker</h2><p>Arrange your badge and a short message on a 57 × 32 mm label.</p></div></div>
<form id="sticker-form"><label for="sticker-message">Message</label><input class="text-input" id="sticker-message" name="message" maxlength="56" value="Thank you for your order" required><p class="sticker-instructions">Drag the badge and message on the label to position them. Use the sliders to resize them.</p>
<div class="sticker-canvas" id="sticker-canvas" aria-label="Interactive 57 by 32 millimetre sticker editor"><img class="sticker-item sticker-badge" id="sticker-badge" src="/static/little-rose-badge-mono.png" alt="Little Rose badge"><div class="sticker-item sticker-message-preview" id="sticker-message-preview">Thank you for your order</div></div>
<input type="hidden" name="logo_x_mm" id="logo-x" value="15"><input type="hidden" name="logo_y_mm" id="logo-y" value="16"><input type="hidden" name="logo_size_mm" id="logo-size-value" value="14"><input type="hidden" name="text_x_mm" id="text-x" value="39"><input type="hidden" name="text_y_mm" id="text-y" value="16"><input type="hidden" name="text_size_pt" id="text-size-value" value="8">
<div class="sticker-controls"><label class="sticker-control" for="logo-size">Badge size <output id="logo-size-output">14 mm</output><input type="range" id="logo-size" min="6" max="27" step="1" value="14"></label><label class="sticker-control" for="text-size">Message size <output id="text-size-output">8 pt</output><input type="range" id="text-size" min="5" max="14" step="0.5" value="8"></label><div class="field"><label for="sticker-copies">Copies</label><input class="sticker-copy" id="sticker-copies" name="copies" type="number" min="1" max="10" value="1" required></div></div>
<button class="submit" id="sticker-preview-button" type="button">Preview sticker <span aria-hidden="true">→</span></button></form><div id="sticker-status" role="status" aria-live="polite"></div></section>
<section class="card" aria-labelledby="pdf-title" style="margin-top:18px"><div class="card-head"><div class="card-icon" aria-hidden="true">↥</div><div><h2 id="pdf-title">Print an existing PDF</h2><p>Use this for carrier labels or other ready-made PDFs.</p></div></div>
<form id="pdf-form"><label for="pdf">Label PDF</label><input id="pdf" name="pdf" type="file" accept="application/pdf,.pdf" required><p class="hint">PDF only · up to 20 MB</p>
<label for="pdf-copies">Copies</label><input class="copies" id="pdf-copies" name="copies" type="number" min="1" max="10" value="1" required>
<button class="submit" id="pdf-submit" type="submit">Print PDF <span aria-hidden="true">→</span></button></form><div id="pdf-status" role="status" aria-live="polite"></div></section>
<p class="note"><span aria-hidden="true">✿</span><span><b>Local printer page</b><br>Available to devices on your home network. Enter an address or upload a ready-made PDF, then collect your label from the Munbyn.</span></p>
</main><footer>Made with care <span>♥</span> by Little Rose</footer>
<dialog id="label-preview" aria-labelledby="preview-title"><div class="preview-head"><h2 id="preview-title">Label preview</h2><button class="preview-close" id="preview-close" type="button" aria-label="Close preview">×</button></div><iframe class="preview-frame" id="preview-frame" title="Preview of the 4 by 6 inch label"></iframe><div class="preview-foot"><p id="preview-message" class="preview-message" role="status" aria-live="polite">Check the address and Little Rose details before printing.</p><div class="preview-actions"><button class="refresh" id="preview-done" type="button">Back to edit</button><button class="submit preview-print" id="preview-print" type="button">Proceed to print <span aria-hidden="true">→</span></button></div></div></dialog>
<dialog class="sticker-dialog" id="sticker-preview-dialog" aria-labelledby="sticker-preview-title"><div class="preview-head"><h2 id="sticker-preview-title">Sticker print preview</h2><button class="preview-close" id="sticker-preview-close" type="button" aria-label="Close preview">×</button></div><iframe class="preview-frame" id="sticker-preview-frame" title="PDF preview of the 57 by 32 millimetre sticker"></iframe><div class="preview-foot"><p class="preview-message" id="sticker-preview-status" role="status" aria-live="polite">Check the design before printing.</p><div class="preview-actions"><button class="refresh" id="sticker-preview-back" type="button">Back to editor</button><button class="submit preview-print" id="sticker-print" type="button">Print sticker <span aria-hidden="true">→</span></button></div></div></dialog>
<script>
const badge=document.querySelector('#queue-badge'),queueMeta=document.querySelector('#queue-meta'),jobList=document.querySelector('#job-list');
const addressForm=document.querySelector('#address-form'),addressStatus=document.querySelector('#address-status'),previewButton=document.querySelector('#preview-label'),previewDialog=document.querySelector('#label-preview'),previewFrame=document.querySelector('#preview-frame'),previewMessage=document.querySelector('#preview-message'),proceedPrintButton=document.querySelector('#preview-print');
let previewSignature='',previewObjectUrl='';
function escapeHtml(value){const el=document.createElement('span');el.textContent=value;return el.innerHTML;}
function formSignature(form){return JSON.stringify([...new FormData(form).entries()]);}
document.querySelector('#fill-from-paste').addEventListener('click',()=>{const feedback=document.querySelector('#paste-feedback');const lines=document.querySelector('#pasted-address').value.split(/\\r?\\n/).map(line=>line.trim()).filter(Boolean);if(lines.length<3){feedback.textContent='Paste at least a name, an address line, and a postcode.';return;}let postcode='';const postcodePattern=/([A-Z]{1,2}[0-9][A-Z0-9]?[ ]*[0-9][A-Z]{2})$/i;const postcodeMatch=lines[lines.length-1].match(postcodePattern);if(postcodeMatch){postcode=postcodeMatch[1].toUpperCase().replace(/[ ]+/g,' ');lines[lines.length-1]=lines[lines.length-1].slice(0,postcodeMatch.index).trim();if(!lines[lines.length-1])lines.pop();}else{postcode=lines.pop();}if(lines.length<2){feedback.textContent='Could not find both a recipient name and address details. Check the pasted text.';return;}const [name,...middle]=lines;const values={name,address_line_1:'',address_line_2:'',town:'',county:'',postcode};if(middle.length===1){values.address_line_1=middle[0];}else if(middle.length===2){[values.address_line_1,values.town]=middle;}else if(middle.length===3){[values.address_line_1,values.town,values.county]=middle;}else{values.address_line_1=middle[0];values.address_line_2=middle[1];values.town=middle[2];values.county=middle.slice(3).join(', ');}for(const [key,value] of Object.entries(values)){const field=addressForm.elements.namedItem(key);if(field)field.value=value;}addressForm.dispatchEvent(new Event('input',{bubbles:true}));addressForm.dispatchEvent(new Event('change',{bubbles:true}));feedback.textContent='Address fields filled. Check the parsed address below and add or correct anything needed.';});
async function refreshQueue(){try{const r=await fetch('/api/status',{cache:'no-store'}),j=await r.json();if(!r.ok)throw new Error(j.detail||'Status unavailable');badge.className='badge'+(j.state==='Printing'?' printing':j.state==='Paused'?' paused':'');badge.textContent=j.state;queueMeta.className='queue-meta';queueMeta.textContent=!j.enabled?'Printer is paused.':j.active_count===0?'Queue is enabled · no jobs waiting.':`${j.active_count} job${j.active_count===1?'':'s'} in the queue.`;jobList.innerHTML=j.jobs.length?j.jobs.map(job=>`<div class="job"><div class="job-main"><div class="job-id">${escapeHtml(job.job_id)}</div><div class="job-detail">${escapeHtml(job.owner)} · ${escapeHtml(job.size)} bytes · ${escapeHtml(job.submitted)}</div></div><div class="job-state">${escapeHtml(job.state)}</div></div>`).join(''):'<div class="job"><div class="job-detail">No recent jobs to show.</div></div>';}catch(err){badge.className='badge error';badge.textContent='Unavailable';queueMeta.className='queue-meta queue-error';queueMeta.textContent=err.message;jobList.innerHTML='';}}
document.querySelector('#refresh').addEventListener('click',refreshQueue);refreshQueue();setInterval(refreshQueue,5000);
for(const input of document.querySelectorAll('.brand-input')){const key='littleRoseLabel:'+input.name;try{const saved=localStorage.getItem(key);if(saved!==null)input.value=saved;}catch{}input.addEventListener('input',()=>{try{localStorage.setItem(key,input.value);}catch{}});}
function updatePreviewPrintState(){proceedPrintButton.disabled=!previewSignature||formSignature(addressForm)!==previewSignature;}
addressForm.addEventListener('input',()=>{updatePreviewPrintState();if(previewSignature&&formSignature(addressForm)!==previewSignature)addressStatus.textContent='Details changed. Preview the updated label before printing.';});
addressForm.addEventListener('change',updatePreviewPrintState);
addressForm.addEventListener('submit',async e=>{e.preventDefault();if(!addressForm.reportValidity())return;previewButton.disabled=true;addressStatus.className='';addressStatus.textContent='Preparing your label preview…';previewMessage.className='preview-message';previewMessage.textContent='Preparing preview…';const signature=formSignature(addressForm);try{const response=await fetch('/api/preview-address',{method:'POST',body:new FormData(addressForm)});if(!response.ok){const body=await response.text();let message=body;try{message=JSON.parse(body).detail||body;}catch{}throw new Error(message||'Could not create preview');}const blob=await response.blob();if(!blob.type.includes('pdf'))throw new Error('The server did not return a PDF preview.');if(previewObjectUrl)URL.revokeObjectURL(previewObjectUrl);previewObjectUrl=URL.createObjectURL(blob);previewFrame.src=previewObjectUrl;previewSignature=signature;updatePreviewPrintState();addressStatus.textContent='Preview ready. Review it before printing.';previewMessage.className='preview-message';previewMessage.textContent='Review the label, then choose Proceed to print.';previewDialog.showModal();}catch(err){addressStatus.className='error';addressStatus.textContent='Could not create preview: '+err.message;}finally{previewButton.disabled=false;}});
function closePreview(){if(previewDialog.open)previewDialog.close();}
previewDialog.addEventListener('close',()=>{previewFrame.removeAttribute('src');if(previewObjectUrl)URL.revokeObjectURL(previewObjectUrl);previewObjectUrl='';previewSignature='';updatePreviewPrintState();});
document.querySelector('#preview-close').addEventListener('click',closePreview);document.querySelector('#preview-done').addEventListener('click',closePreview);
proceedPrintButton.addEventListener('click',async()=>{if(!previewSignature||formSignature(addressForm)!==previewSignature){previewMessage.className='preview-message error';previewMessage.textContent='The details changed. Go back and preview the updated label.';proceedPrintButton.disabled=true;return;}const summary=['recipient-name','address-line-1','address-line-2','town','county','postcode'].map(id=>document.getElementById(id).value.trim()).filter(Boolean).join(String.fromCharCode(10));const copies=document.querySelector('#address-copies').value;if(!window.confirm(`Send this label to the Munbyn printer?\n\n${summary}\n\nCopies: ${copies}`)){previewMessage.className='preview-message';previewMessage.textContent='Printing cancelled. The label has not been sent.';return;}previewMessage.className='preview-message';previewMessage.textContent='Sending the label to the Munbyn…';proceedPrintButton.disabled=true;previewButton.disabled=true;try{const response=await fetch('/api/print-address',{method:'POST',body:new FormData(addressForm)});const body=await response.text();let result={};try{result=JSON.parse(body);}catch{}if(!response.ok)throw new Error(result.detail||body||'Print request failed');closePreview();addressStatus.className='';addressStatus.textContent='Address label sent to the printer. Job: '+result.job_id;for(const id of ['recipient-name','address-line-1','address-line-2','town','county','postcode'])document.getElementById(id).value='';document.querySelector('#address-copies').value='1';previewSignature='';updatePreviewPrintState();}catch(err){previewMessage.className='preview-message error';previewMessage.textContent='Could not print: '+err.message;proceedPrintButton.disabled=false;}finally{previewButton.disabled=false;refreshQueue();}});
function bindPrintForm(formId,endpoint,statusId,buttonId,successText){const form=document.querySelector(formId),status=document.querySelector(statusId),button=document.querySelector(buttonId);form.addEventListener('submit',async e=>{e.preventDefault();status.className='';status.textContent='Sending to the Munbyn…';button.disabled=true;try{const r=await fetch(endpoint,{method:'POST',body:new FormData(form)}),j=await r.json();if(!r.ok)throw new Error(j.detail||'Print request failed');status.textContent=successText+' Job: '+j.job_id;form.reset();}catch(err){status.className='error';status.textContent='Could not print: '+err.message;}finally{button.disabled=false;refreshQueue();}});}
const stickerForm=document.querySelector('#sticker-form'),stickerCanvas=document.querySelector('#sticker-canvas'),stickerBadge=document.querySelector('#sticker-badge'),stickerMessage=document.querySelector('#sticker-message-preview'),stickerDialog=document.querySelector('#sticker-preview-dialog'),stickerPreviewFrame=document.querySelector('#sticker-preview-frame'),stickerStatus=document.querySelector('#sticker-status');
let stickerPreviewUrl='';
function updateStickerCanvas(){
  const logoSize=Number(document.querySelector('#logo-size').value),textSize=Number(document.querySelector('#text-size').value),width=stickerCanvas.clientWidth;
  stickerBadge.style.left=(Number(document.querySelector('#logo-x').value)/57*100)+'%';stickerBadge.style.top=(Number(document.querySelector('#logo-y').value)/32*100)+'%';stickerBadge.style.width=(logoSize/57*100)+'%';
  stickerMessage.style.left=(Number(document.querySelector('#text-x').value)/57*100)+'%';stickerMessage.style.top=(Number(document.querySelector('#text-y').value)/32*100)+'%';stickerMessage.style.fontSize=(textSize/72*25.4/57*width)+'px';
  stickerMessage.textContent=document.querySelector('#sticker-message').value||'Your message';
  document.querySelector('#logo-size-value').value=logoSize;document.querySelector('#text-size-value').value=textSize;
  document.querySelector('#logo-size-output').textContent=logoSize+' mm';document.querySelector('#text-size-output').textContent=textSize+' pt';
}
for(const id of ['sticker-message','logo-size','text-size'])document.getElementById(id).addEventListener('input',updateStickerCanvas);
function bindStickerDrag(element,xId,yId){
  element.addEventListener('pointerdown',event=>{event.preventDefault();element.setPointerCapture(event.pointerId);});
  element.addEventListener('pointermove',event=>{if(!element.hasPointerCapture(event.pointerId))return;const rect=stickerCanvas.getBoundingClientRect();document.getElementById(xId).value=Math.max(0,Math.min(57,(event.clientX-rect.left)/rect.width*57)).toFixed(1);document.getElementById(yId).value=Math.max(0,Math.min(32,(event.clientY-rect.top)/rect.height*32)).toFixed(1);updateStickerCanvas();});
}
bindStickerDrag(stickerBadge,'logo-x','logo-y');bindStickerDrag(stickerMessage,'text-x','text-y');window.addEventListener('resize',updateStickerCanvas);updateStickerCanvas();
document.querySelector('#sticker-preview-button').addEventListener('click',async()=>{
  if(!stickerForm.reportValidity())return;const button=document.querySelector('#sticker-preview-button');button.disabled=true;stickerStatus.className='';stickerStatus.textContent='Preparing sticker preview…';
  try{const response=await fetch('/api/preview-sticker',{method:'POST',body:new FormData(stickerForm)});if(!response.ok){const body=await response.text();let message=body;try{message=JSON.parse(body).detail||body;}catch{}throw new Error(message||'Could not create preview');}const blob=await response.blob();if(!blob.type.includes('pdf'))throw new Error('The server did not return a PDF preview.');if(stickerPreviewUrl)URL.revokeObjectURL(stickerPreviewUrl);stickerPreviewUrl=URL.createObjectURL(blob);stickerPreviewFrame.src=stickerPreviewUrl;document.querySelector('#sticker-preview-status').textContent='Check the design and label size, then choose Print sticker.';stickerStatus.textContent='Preview ready.';stickerDialog.showModal();}catch(error){stickerStatus.className='error';stickerStatus.textContent='Could not create preview: '+error.message;}finally{button.disabled=false;}
});
function closeStickerPreview(){if(stickerDialog.open)stickerDialog.close();}
stickerDialog.addEventListener('close',()=>{stickerPreviewFrame.removeAttribute('src');if(stickerPreviewUrl)URL.revokeObjectURL(stickerPreviewUrl);stickerPreviewUrl='';});
document.querySelector('#sticker-preview-close').addEventListener('click',closeStickerPreview);document.querySelector('#sticker-preview-back').addEventListener('click',closeStickerPreview);
document.querySelector('#sticker-print').addEventListener('click',async()=>{
  const message=document.querySelector('#sticker-message').value.trim(),copies=document.querySelector('#sticker-copies').value;if(!window.confirm(`Send this 57 × 32 mm sticker to the Munbyn?\n\n${message}\n\nCopies: ${copies}`))return;
  const button=document.querySelector('#sticker-print');button.disabled=true;document.querySelector('#sticker-preview-status').textContent='Sending sticker to the Munbyn…';
  try{const response=await fetch('/api/print-sticker',{method:'POST',body:new FormData(stickerForm)}),body=await response.text();let result={};try{result=JSON.parse(body);}catch{}if(!response.ok)throw new Error(result.detail||body||'Print request failed');closeStickerPreview();stickerStatus.className='';stickerStatus.textContent='Sticker sent to the printer. Job: '+result.job_id;refreshQueue();}catch(error){document.querySelector('#sticker-preview-status').className='preview-message error';document.querySelector('#sticker-preview-status').textContent='Could not print: '+error.message;}finally{button.disabled=false;}
});
bindPrintForm('#pdf-form','/api/print-from-page','#pdf-status','#pdf-submit','PDF sent to the printer.');
</script></body></html>""")


async def submit_pdf(pdf: UploadFile, copies: int):
    if not 1 <= copies <= 10:
        raise HTTPException(status_code=400, detail="Copies must be between 1 and 10")
    if Path(pdf.filename or "").suffix.lower() != ".pdf":
        raise HTTPException(status_code=415, detail="Upload a PDF file")

    data = await pdf.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF must be 20 MB or smaller")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="The uploaded file is not a valid PDF")

    return await submit_bytes_with_power(data, copies)


def submit_bytes(data: bytes, copies: int, print_option: str = "PageSize=w288h432"):
    if print_option not in {"PageSize=w288h432", "media=Custom.57x32mm"}:
        raise HTTPException(status_code=400, detail="Unsupported label size")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(prefix="little-rose-", suffix=".pdf", delete=False) as temp:
            temp.write(data)
            temp_name = temp.name
        result = subprocess.run(
            ["lp", "-d", QUEUE, "-o", print_option, "-n", str(copies), temp_name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise HTTPException(status_code=502, detail="CUPS rejected the print job: " + result.stderr[-500:])
        match = re.search(r"request id is (\S+)", result.stdout)
        return {"status": "submitted", "queue": QUEUE, "job_id": match.group(1) if match else result.stdout.strip()}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Timed out while submitting the print job")
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


@app.post("/api/print-from-page")
async def print_from_page(
    request: Request,
    pdf: UploadFile = File(...),
    copies: int = Form(1),
):
    validate_page_origin(request)
    return await submit_pdf(pdf, copies)


@app.post("/api/print-address")
async def print_address_from_page(
    request: Request,
    name: str = Form(...),
    address_line_1: str = Form(...),
    address_line_2: str = Form(""),
    town: str = Form(...),
    county: str = Form(""),
    postcode: str = Form(...),
    instagram: str = Form("@little.rose_shop"),
    facebook: str = Form("@littlerosegifts"),
    website: str = Form("www.littlerose.shop"),
    copies: int = Form(1),
):
    validate_page_origin(request)
    if not 1 <= copies <= 10:
        raise HTTPException(status_code=400, detail="Copies must be between 1 and 10")
    label_pdf = build_address_label_pdf(
        name, address_line_1, address_line_2, town, county, postcode, instagram, facebook, website
    )
    return await submit_bytes_with_power(label_pdf, copies)


@app.post("/api/preview-sticker")
async def preview_sticker_from_page(
    request: Request,
    message: str = Form(...),
    logo_x_mm: float = Form(15),
    logo_y_mm: float = Form(16),
    logo_size_mm: float = Form(14),
    text_x_mm: float = Form(39),
    text_y_mm: float = Form(16),
    text_size_pt: float = Form(8),
):
    validate_page_origin(request)
    pdf_data = create_sticker_pdf(message, logo_x_mm, logo_y_mm, logo_size_mm, text_x_mm, text_y_mm, text_size_pt)
    return Response(
        content=pdf_data,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=little-rose-sticker-preview.pdf", "Cache-Control": "no-store"},
    )


@app.post("/api/print-sticker")
async def print_sticker_from_page(
    request: Request,
    message: str = Form(...),
    logo_x_mm: float = Form(15),
    logo_y_mm: float = Form(16),
    logo_size_mm: float = Form(14),
    text_x_mm: float = Form(39),
    text_y_mm: float = Form(16),
    text_size_pt: float = Form(8),
    copies: int = Form(1),
):
    validate_page_origin(request)
    if not 1 <= copies <= 10:
        raise HTTPException(status_code=400, detail="Copies must be between 1 and 10")
    pdf_data = create_sticker_pdf(message, logo_x_mm, logo_y_mm, logo_size_mm, text_x_mm, text_y_mm, text_size_pt)
    return await submit_bytes_with_power(pdf_data, copies, "media=Custom.57x32mm")


@app.post("/api/preview-address")
async def preview_address_from_page(
    request: Request,
    name: str = Form(...),
    address_line_1: str = Form(...),
    address_line_2: str = Form(""),
    town: str = Form(...),
    county: str = Form(""),
    postcode: str = Form(...),
    instagram: str = Form("@little.rose_shop"),
    facebook: str = Form("@littlerosegifts"),
    website: str = Form("www.littlerose.shop"),
):
    validate_page_origin(request)
    pdf_data = build_address_label_pdf(
        name, address_line_1, address_line_2, town, county, postcode, instagram, facebook, website
    )
    return Response(
        content=pdf_data,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=little-rose-label-preview.pdf", "Cache-Control": "no-store"},
    )


@app.post("/api/print-address-api")
async def print_address_api(payload: AddressLabelRequest, authorization: str | None = Header(default=None)):
    """Authenticated server-to-server endpoint for printing the branded address label."""
    authorize(authorization)
    if not 1 <= payload.copies <= 10:
        raise HTTPException(status_code=400, detail="Copies must be between 1 and 10")
    label_pdf = build_address_label_pdf(
        payload.name,
        payload.address_line_1,
        payload.address_line_2,
        payload.town,
        payload.county,
        payload.postcode,
        payload.instagram,
        payload.facebook,
        payload.website,
    )
    return await submit_bytes_with_power(label_pdf, payload.copies)


@app.get("/api/print-jobs/{job_id}")
def print_job_status(job_id: str, authorization: str | None = Header(default=None)):
    """Return the current CUPS queue/history status for a job created by this service."""
    authorize(authorization)
    if not re.fullmatch(rf"{re.escape(QUEUE)}-\d+", job_id):
        raise HTTPException(status_code=400, detail="Invalid print job ID")

    active_jobs = read_jobs("not-completed")
    active_job = next((job for job in active_jobs if job["job_id"] == job_id), None)
    if active_job:
        printer_result = subprocess.run(["lpstat", "-p", QUEUE], capture_output=True, text=True, timeout=10)
        if printer_result.returncode:
            raise HTTPException(status_code=503, detail="Could not read printer status")
        current_match = re.search(r"now printing\s+(\S+)", printer_result.stdout, re.IGNORECASE)
        state = "printing" if current_match and current_match.group(1) == job_id else "queued"
        return {
            "job_id": job_id,
            "status": state,
            "queue": QUEUE,
            "message": "CUPS reports this job is currently printing." if state == "printing" else "CUPS reports this job is queued.",
        }

    completed_jobs = read_jobs("completed")
    if any(job["job_id"] == job_id for job in completed_jobs):
        return {
            "job_id": job_id,
            "status": "completed",
            "queue": QUEUE,
            "message": "CUPS lists this job in completed history; this does not confirm the physical label output.",
        }
    return {
        "job_id": job_id,
        "status": "unknown",
        "queue": QUEUE,
        "message": "CUPS no longer reports this job as queued or in retained completed history.",
    }


@app.post("/api/print")
async def print_pdf(
    pdf: UploadFile = File(...),
    copies: int = Form(1),
    authorization: str | None = Header(default=None),
):
    authorize(authorization)
    return await submit_pdf(pdf, copies)
