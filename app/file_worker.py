"""Bounded document extraction subprocess. No network or user code execution."""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import posixpath
import re
import resource
import subprocess
import warnings
import wave
import zipfile
from pathlib import Path, PurePosixPath

MAX_TEXT = 250_000
MAX_PDF_PAGES = 100
MAX_VISUAL_PAGES = 8
MAX_IMAGES_BYTES = 8 * 1024 * 1024


class FileFailure(Exception):
    pass


def text_part(text):
    if len(text) > MAX_TEXT:
        raise FileFailure("file_extraction_limit")
    return {"type": "text", "text": text}


def image_part(data, mime="image/jpeg"):
    if len(data) > 4 * 1024 * 1024:
        raise FileFailure("file_extraction_limit")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode()}}


def command(args, *, timeout=25):
    return subprocess.run(args, capture_output=True, check=True, timeout=timeout).stdout


def ocr(path):
    return command(["tesseract", str(path), "stdout", "-l", os.environ.get("OCR_LANGUAGES", "eng")]).decode("utf-8").strip()


def raster(path, mode):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 16_000_000
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with Image.open(path, formats=["PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"]) as source:
        if source.width * source.height > 16_000_000:
            raise FileFailure("file_extraction_limit")
        source.load()
        frame_count = getattr(source, "n_frames", 1)
        original_format = source.format
        image = source.convert("RGB")
        image.thumbnail((2048, 2048))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=92)
        data = buffer.getvalue()
        normalized = path.with_suffix(".jpg")
        normalized.write_bytes(data)
    if mode == "text":
        content = ocr(normalized)
        if not content:
            raise FileFailure("file_has_no_extractable_text")
        parts = [text_part(content)]
    else:
        parts = [image_part(data)]
    return {"parts": parts, "metadata": {"kind": "image", "width": image.width, "height": image.height, "frames": frame_count, "original_format": original_format}, "warnings": ["Only the first animation frame is analyzed."] if frame_count > 1 else []}


def pdf(path, mode):
    if not path.read_bytes()[:8].startswith(b"%PDF-"):
        raise FileFailure("invalid_file")
    information = command(["pdfinfo", str(path)]).decode("utf-8", errors="replace")
    if re.search(r"Encrypted:\s+yes", information):
        raise FileFailure("encrypted_file_not_supported")
    match = re.search(r"Pages:\s+(\d+)", information)
    if match is None or not 1 <= int(match[1]) <= MAX_PDF_PAGES:
        raise FileFailure("file_extraction_limit")
    count = int(match[1])
    raw = command(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"]).decode("utf-8")
    if len(raw) > MAX_TEXT:
        raise FileFailure("file_extraction_limit")
    pages = raw.split("\f")[:count]
    pages.extend([""] * (count - len(pages)))
    visual = list(range(count)) if mode == "vision" else [index for index, value in enumerate(pages) if not value.strip()] if mode == "auto" else []
    if len(visual) > MAX_VISUAL_PAGES:
        raise FileFailure("file_visual_page_limit")
    parts, notices = [], []
    image_bytes = 0
    for index, value in enumerate(pages):
        content = value.strip()
        if index in visual:
            prefix = path.parent / f"page-{index + 1}"
            command(["pdftoppm", "-f", str(index + 1), "-l", str(index + 1), "-singlefile", "-scale-to", "1024", "-jpeg", str(path), str(prefix)])
            rendered = prefix.with_suffix(".jpg")
            data = rendered.read_bytes()
            image_bytes += len(data)
            if image_bytes > MAX_IMAGES_BYTES:
                raise FileFailure("file_extraction_limit")
            if not content:
                try:
                    content = ocr(rendered)
                except (FileNotFoundError, subprocess.SubprocessError):
                    notices.append(f"OCR unavailable on page {index + 1}; visual input retained.")
            parts.append(text_part(f"[page {index + 1}]\n{content}"))
            parts.append(image_part(data))
        else:
            if not content:
                notices.append(f"No extractable text on page {index + 1}.")
            parts.append(text_part(f"[page {index + 1}]\n{content or '[no extracted text]'}"))
    if sum(len(part.get("text", "")) for part in parts) > MAX_TEXT:
        raise FileFailure("file_extraction_limit")
    return {"parts": parts, "metadata": {"kind": "pdf", "pages": count, "visual_pages": [index + 1 for index in visual]}, "warnings": notices}


def office(path, kind):
    from defusedxml import ElementTree
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 2000 or sum(item.file_size for item in infos) > 50 * 1024 * 1024:
            raise FileFailure("file_extraction_limit")
        for item in infos:
            if item.file_size > 10 * 1024 * 1024 or item.flag_bits & 1 or item.file_size > max(1, item.compress_size) * 1000:
                raise FileFailure("file_extraction_limit")
            if PurePosixPath(item.filename).is_absolute() or ".." in PurePosixPath(item.filename).parts:
                raise FileFailure("invalid_file")

        def xml(name):
            return ElementTree.fromstring(archive.read(name))

        lines = []
        if kind == "docx":
            namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            root = xml("word/document.xml")
            for index, paragraph in enumerate(root.findall(".//w:p", namespace), 1):
                lines.append(f"[paragraph {index}] " + "".join(part.text or "" for part in paragraph.findall(".//w:t", namespace)))
        elif kind == "pptx":
            slides = sorted((item.filename for item in infos if re.fullmatch(r"ppt/slides/slide\d+\.xml", item.filename)), key=lambda value: int(re.search(r"slide(\d+)", value)[1]))
            if len(slides) > 100:
                raise FileFailure("file_extraction_limit")
            for index, slide in enumerate(slides, 1):
                lines.append(f"[slide {index}]\n" + "\n".join(element.text or "" for element in xml(slide).iter() if element.tag.endswith("}t")))
        elif kind == "xlsx":
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared = ["".join(node.text or "" for node in element.iter() if node.tag.endswith("}t")) for element in xml("xl/sharedStrings.xml")]
            names = archive.namelist()
            sheets = [(PurePosixPath(name).stem, name) for name in sorted(names) if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)]
            if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
                relationships = {element.get("Id"): element for element in xml("xl/_rels/workbook.xml.rels")}
                sheets = []
                for sheet in xml("xl/workbook.xml").iter():
                    if not sheet.tag.endswith("}sheet"):
                        continue
                    relationship = relationships.get(sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"))
                    if relationship is None or relationship.get("TargetMode") == "External":
                        raise FileFailure("invalid_file")
                    target = relationship.get("Target", "")
                    target = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
                    if not target.startswith("xl/worksheets/") or target not in names or "\\" in target:
                        raise FileFailure("invalid_file")
                    sheets.append((sheet.get("name", PurePosixPath(target).stem), target))
            total = 0
            for name, sheet in sheets:
                lines.append(f"[worksheet {name}]")
                for cell in xml(sheet).iter():
                    if not cell.tag.endswith("}c"):
                        continue
                    total += 1
                    if total > 10_000:
                        raise FileFailure("file_extraction_limit")
                    value = next((node.text or "" for node in cell if node.tag.endswith("}v")), "")
                    if cell.get("t") == "s":
                        value = shared[int(value)]
                    elif cell.get("t") == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter() if node.tag.endswith("}t"))
                    formula = next((node.text for node in cell if node.tag.endswith("}f")), None)
                    if formula:
                        value = f"={formula} [cached value: {value}; not recalculated]"
                    lines.append(f"{cell.get('r', '?')}: {value}")
        else:
            root = xml("content.xml")
            lines = ["".join(node.itertext()) for node in root.iter() if node.tag.endswith(("}p", "}h"))]
        text = "\n".join(lines)
        if not text.strip():
            raise FileFailure("file_has_no_extractable_text")
        return {"parts": [text_part(text)], "metadata": {"kind": kind, "extraction": "text"}, "warnings": ["Embedded images, layout and spreadsheet recalculation are not analyzed by text extraction."]}


MEDIA_INPUT_OPTIONS = ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm,mp3,wav,flac,ogg,aac"]


def probe_media(path):
    information = json.loads(command(["ffprobe", "-v", "error", *MEDIA_INPUT_OPTIONS, "-show_format", "-show_streams", "-of", "json", str(path)]))
    duration = float(information.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise FileFailure("invalid_file")
    return information, duration


def video(path, mode):
    information, duration = probe_media(path)
    if duration > float(os.environ.get("VIDEO_MAX_SECONDS", "120")):
        raise FileFailure("file_extraction_limit")
    if mode == "text":
        result = audio(path, mode)
        result["metadata"]["kind"] = "video"
        result["metadata"]["extraction"] = "audio_transcript"
        result["warnings"].append("Video visuals are not analyzed in text mode.")
        return result
    stream = next((item for item in information["streams"] if item.get("codec_type") == "video"), None)
    if stream is None:
        raise FileFailure("invalid_file")
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    if not width or not height or width * height > 16_000_000:
        raise FileFailure("file_extraction_limit")
    requested = int(os.environ.get("VIDEO_FRAME_COUNT", "4"))
    known_frames = stream.get("nb_frames", "")
    count = min(requested, int(known_frames)) if str(known_frames).isdigit() and int(known_frames) > 0 else requested
    samples = [round(duration * index / count, 3) for index in range(count)]
    parts, size = [], 0
    for index, timestamp in enumerate(samples):
        output = path.parent / f"frame-{index}.jpg"
        command(["ffmpeg", "-v", "error", *MEDIA_INPUT_OPTIONS, "-threads", "2", "-ss", str(timestamp), "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", "1", "-vf", "scale=1024:1024:force_original_aspect_ratio=decrease", "-filter_threads", "1", "-threads", "2", "-q:v", "3", "-y", str(output)])
        data = output.read_bytes()
        size += len(data)
        if size > MAX_IMAGES_BYTES:
            raise FileFailure("file_extraction_limit")
        parts.append(text_part(f"[video frame sampled near {timestamp:.3f} seconds]"))
        parts.append(image_part(data))
    audio_present = any(item.get("codec_type") == "audio" for item in information["streams"])
    notices = ["Analysis uses sampled frames, not every video frame."]
    if audio_present:
        notices.append("The audio track has not been transcribed.")
    return {"parts": parts, "metadata": {"kind": "video", "duration_seconds": duration, "sampled_times_seconds": samples, "audio_present": audio_present}, "warnings": notices}


def audio(path, mode):
    if mode == "vision":
        raise FileFailure("file_mode_not_supported")
    information, duration = probe_media(path)
    maximum = float(os.environ.get("AUDIO_MAX_SECONDS", "300"))
    if duration > maximum or not any(item.get("codec_type") == "audio" for item in information["streams"]):
        raise FileFailure("file_extraction_limit")
    normalized = path.parent / "normalized.wav"
    command(["ffmpeg", "-v", "error", *MEDIA_INPUT_OPTIONS, "-threads", "2", "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn", "-t", str(maximum + 0.1), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-threads", "2", "-y", str(normalized)])
    with wave.open(str(normalized), "rb") as sound:
        actual = sound.getnframes() / sound.getframerate()
    if actual > maximum + 0.05 or normalized.stat().st_size > 20 * 1024 * 1024:
        raise FileFailure("file_extraction_limit")
    return {"parts": [], "normalized_audio": base64.b64encode(normalized.read_bytes()).decode(), "metadata": {"kind": "audio", "duration_seconds": actual}, "warnings": []}


def extract(path, kind, mode):
    if kind == "image":
        return raster(path, mode)
    if kind == "pdf":
        return pdf(path, mode)
    if kind in {"docx", "xlsx", "pptx", "odt"}:
        if mode == "vision":
            raise FileFailure("file_mode_not_supported")
        return office(path, kind)
    if kind == "video":
        return video(path, mode)
    if kind == "audio":
        return audio(path, mode)
    if kind == "text":
        data = path.read_bytes()
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        text = data.decode(encoding)
        if "\x00" in text:
            raise FileFailure("invalid_file")
        return {"parts": [text_part(text)], "metadata": {"kind": "text"}, "warnings": []}
    raise FileFailure("file_type_not_supported")


def main():
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (55, 55))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("kind")
    parser.add_argument("mode", choices=["auto", "text", "vision"])
    args = parser.parse_args()
    try:
        result = extract(args.path, args.kind, args.mode)
    except FileFailure as exc:
        result = {"error": str(exc)}
    except FileNotFoundError:
        result = {"error": "file_processing_unavailable"}
    except Exception:  # noqa: BLE001 -- untrusted parser failures must not print document content
        result = {"error": "invalid_file"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
