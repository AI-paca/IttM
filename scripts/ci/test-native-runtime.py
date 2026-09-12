#!/usr/bin/env python3
"""Exercise the actual Rust executable, native Tesseract, PDF and HTTP contracts.

The Python implementation remains the differential oracle; tests are not ported.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get("ITTM_OCR_BIN", ROOT / "ocr-runtime/target/release/ittm-ocr"))
sys.path.insert(0, str(ROOT / "ocr"))
from app.services.convert_service import convert_bytes


def run(subcommand, *options):
    if subcommand not in {"convert", "debug", "check"}:
        raise ValueError("Only native regression commands are allowed")
    command = [str(BINARY), subcommand, *map(str, options)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def native(path, pdf_mode="auto"):
    events = [json.loads(line) for line in run("convert", path, "--engine", "tesseract", "--pdf-mode", pdf_mode, "--stream").splitlines()]
    assert events[-1]["type"] == "complete", events
    return "\n\n".join(e["markdown"] for e in events if e["type"] == "page" and e["markdown"].strip()), events[-1]["meta"]


def write_pdf(path):
    lines = ["A readable document contains ordinary English words and useful information."] * 8
    commands = "BT /F1 14 Tf 40 740 Td 22 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(commands)} >>\nstream\n{commands}\nendstream".encode(),
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)


def main():
    fonts = [Path("/usr/share/fonts/TTF/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    font = ImageFont.truetype(str(next(p for p in fonts if p.exists())), 32)
    with tempfile.TemporaryDirectory(prefix="ittm-native-regression-") as directory:
        work = Path(directory)
        image = Image.new("RGB", (1000, 260), "white")
        draw = ImageDraw.Draw(image)
        draw.text((40, 30), "Native Rust document pipeline", font=font, fill="black")
        draw.text((40, 100), "Invoice total 12345.67", font=font, fill="black")
        draw.text((40, 170), "Документ на русском языке", font=font, fill="black")
        image.save(work / "text.png")
        alpha = Image.new("RGBA", image.size, (0, 0, 0, 0))
        mask = image.convert("L").point(lambda p: 255 - p)
        alpha.putalpha(mask)
        alpha.save(work / "alpha.png")
        table = Image.new("RGB", (800, 280), "white")
        draw = ImageDraw.Draw(table)
        for y in (20, 130, 260):
            draw.line((20, y, 780, y), fill="black", width=2)
        for x in (20, 400, 780):
            draw.line((x, 20, x, 260), fill="black", width=2)
        for x, y, text in ((40, 45, "Product"), (430, 45, "Amount"), (40, 165, "Invoice"), (430, 165, "12500")):
            draw.text((x, y), text, font=font, fill="black")
        table.save(work / "table.png")
        for name in ("text.png", "alpha.png", "table.png"):
            path = work / name
            expected, _ = convert_bytes(path.read_bytes(), filename=name, engine_type="tesseract")
            actual, meta = native(path)
            assert actual == expected, (name, expected, actual)
            assert meta["pipeline"] == "rust_separated_v1"
            print(f"PASS Python/Rust OCR parity: {name}", flush=True)

        pdf = work / "text.pdf"
        write_pdf(pdf)
        expected, _ = convert_bytes(pdf.read_bytes(), filename=pdf.name, engine_type="tesseract")
        actual, meta = native(pdf)
        assert actual == expected, (expected, actual)
        assert meta["pipeline"] == "pdf_text_layer"
        print("PASS trusted PDF text-layer parity", flush=True)
        image.save(work / "raster.pdf", resolution=150)
        actual, meta = native(work / "raster.pdf", "raster")
        assert "Native Rust" in actual and meta["pipeline"] == "rust_separated_v1"
        print("PASS forced PDF raster conversion", flush=True)

        run("debug", work / "text.png", "--output", work / "checkpoint")
        run("debug", work / "text.png", "--output", work / "replay", "--resume", work / "checkpoint/checkpoint.json")
        assert (work / "checkpoint/result.md").read_bytes() == (work / "replay/result.md").read_bytes()
        print("PASS native checkpoint replay", flush=True)

        port_file = work / "port"
        process = subprocess.Popen([str(BINARY), "serve", "--port", "0", "--port-file", str(port_file)], stdout=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not port_file.exists() and time.monotonic() < deadline:
                assert process.poll() is None, "Server stopped during startup"
                time.sleep(0.025)
            base = "http://127.0.0.1:" + port_file.read_text()
            assert json.load(urllib.request.urlopen(base + "/readiness"))["ready"]
            body = b'--ittm\r\nContent-Disposition: form-data; name="file"; filename="image.png"\r\nContent-Type: image/png\r\n\r\n' + (work / "text.png").read_bytes() + b"\r\n--ittm--\r\n"
            headers = {"Content-Type": "multipart/form-data; boundary=ittm"}
            response = json.load(urllib.request.urlopen(urllib.request.Request(base + "/v1/convert?engine_type=tesseract", data=body, headers=headers), timeout=120))
            assert response["markdown"] == native(work / "text.png")[0]
            with urllib.request.urlopen(urllib.request.Request(base + "/convert/stream?engine_type=tesseract", data=body, headers=headers), timeout=120) as stream:
                events = [json.loads(line) for line in stream]
            assert events[-1]["type"] == "complete"
            assert any(e["type"] == "page" for e in events)
            print("PASS HTTP multipart and NDJSON stream contracts", flush=True)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
