#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a narrated course video from lecture HTML and local CosyVoice audio.

Config driven pipeline:
  HTML slides -> PNG screenshots
  segment text -> CosyVoice wav -> magnetic voice processing
  slide clips + optional demo clips -> final MP4
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


MAGNETIC_FILTER = (
    "highpass=f=65,"
    "equalizer=f=120:t=q:w=1.0:g=3.5,"
    "equalizer=f=200:t=q:w=1.2:g=2,"
    "equalizer=f=3000:t=q:w=2.0:g=-1.5,"
    "equalizer=f=7500:t=q:w=2.5:g=-3.5,"
    "highshelf=f=9000:g=-2.5,"
    "acompressor=threshold=-19dB:ratio=3:attack=10:release=200:makeup=2.5,"
    "loudnorm=I=-16:TP=-1.5:LRA=11"
)

DEFAULT_CONFIG: dict[str, Any] = {
    "cosyvoice_root": "D:/CosyVoice",
    "model_dir": "pretrained_models/CosyVoice2-0.5B",
    "prompt_wav": "work/congge_prompt_16k.wav",
    "instruct_text": "用沉稳低沉、富有磁性和感染力的语气，沉着有力、略带激情但克制地娓娓道来<|endofprompt|>",
    "html": "C:/path/to/lesson.html",
    "work_dir": "D:/CosyVoice/work/lesson_video",
    "output_video": "C:/path/to/final.mp4",
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "audio_filter": MAGNETIC_FILTER,
    "segments": [
        {"type": "slide", "slide": 1, "text": "第一段口播稿。"},
        {"type": "slide", "slide": 2, "text": "第二段口播稿。"},
        {"type": "demo", "path": "C:/path/to/software-demo.mp4"},
        {"type": "slide", "slide": 3, "text": "演示之后的总结。"},
    ],
}


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(data)
    return cfg


def resolve(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else root / path


def ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def run(cmd: list[str], *, quiet: bool = False) -> None:
    if quiet:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        print(" ".join(map(str, cmd)))
        subprocess.run(cmd, check=True)


def dirs(cfg: dict[str, Any]) -> dict[str, Path]:
    work = Path(cfg["work_dir"])
    paths = {
        "work": work,
        "slides": work / "slides",
        "audio_raw": work / "audio_raw",
        "audio_final": work / "audio_final",
        "clips": work / "clips",
        "qc": work / "qc",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def slide_count(cfg: dict[str, Any]) -> int:
    slides = [int(seg["slide"]) for seg in cfg["segments"] if seg.get("type", "slide") == "slide"]
    return max(slides) if slides else 0


def render_slides(cfg: dict[str, Any], *, force: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    paths = dirs(cfg)
    html = Path(cfg["html"]).resolve()
    if not html.exists():
        raise FileNotFoundError(html)
    count = slide_count(cfg)
    width, height = int(cfg["width"]), int(cfg["height"])

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        page.goto(html.as_uri(), wait_until="networkidle")
        page.add_style_tag(content=".controls,.progress-bar,.laser-pointer{display:none!important}")
        page.wait_for_timeout(900)
        for i in range(1, count + 1):
            out = paths["slides"] / f"slide_{i:02d}.png"
            if force or not out.exists():
                page.screenshot(path=str(out), full_page=False)
                print("shot", out)
            if i != count:
                page.keyboard.press("ArrowRight")
                page.wait_for_timeout(850)
        browser.close()


def slide_segments(cfg: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    items: list[tuple[int, dict[str, Any]]] = []
    for idx, seg in enumerate(cfg["segments"], start=1):
        if seg.get("type", "slide") == "slide":
            items.append((idx, seg))
    return items


def generate_voice(cfg: dict[str, Any], *, force: bool = False) -> None:
    import soundfile as sf
    import torch

    paths = dirs(cfg)
    root = Path(cfg["cosyvoice_root"])
    model_dir = resolve(root, cfg.get("model_dir"))
    prompt_wav = resolve(root, cfg.get("prompt_wav"))
    if not root.exists():
        raise FileNotFoundError(root)
    if model_dir is None or not model_dir.exists():
        raise FileNotFoundError(model_dir)
    if prompt_wav is None or not prompt_wav.exists():
        raise FileNotFoundError(prompt_wav)

    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "third_party" / "Matcha-TTS"))
    from cosyvoice.cli.cosyvoice import AutoModel

    cv = AutoModel(model_dir=str(model_dir))
    sr = cv.sample_rate
    instruct = cfg["instruct_text"]
    audio_filter = cfg.get("audio_filter") or MAGNETIC_FILTER

    for seq, seg in slide_segments(cfg):
        raw = paths["audio_raw"] / f"seg_{seq:02d}.wav"
        final = paths["audio_final"] / f"seg_{seq:02d}_mag.wav"
        if force or not raw.exists():
            chunks = []
            for chunk in cv.inference_instruct2(seg["text"], instruct, str(prompt_wav), stream=False, text_frontend=False):
                chunks.append(chunk["tts_speech"])
            audio = torch.concat(chunks, dim=1)
            sf.write(raw, audio.squeeze(0).cpu().numpy(), sr)
            print(f"voice seg_{seq:02d}: {audio.shape[1] / sr:.1f}s -> {raw}")
        if force or not final.exists():
            run([
                ffmpeg(),
                "-y",
                "-i",
                str(raw),
                "-af",
                audio_filter,
                "-ar",
                "48000",
                "-ac",
                "2",
                str(final),
            ], quiet=True)
            print("audio", final)


def slide_clip(cfg: dict[str, Any], seq: int, seg: dict[str, Any], *, force: bool = False) -> Path:
    import soundfile as sf

    paths = dirs(cfg)
    width, height, fps = int(cfg["width"]), int(cfg["height"]), int(cfg["fps"])
    slide = int(seg["slide"])
    img = paths["slides"] / f"slide_{slide:02d}.png"
    audio = paths["audio_final"] / f"seg_{seq:02d}_mag.wav"
    out = paths["clips"] / f"part_{seq:02d}_slide.mp4"
    if out.exists() and not force:
        return out
    if not img.exists():
        raise FileNotFoundError(img)
    if not audio.exists():
        raise FileNotFoundError(audio)
    duration = sf.info(str(audio)).duration + float(seg.get("tail_pad", 0.35))
    vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    run([
        ffmpeg(),
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(fps),
        "-i",
        str(img),
        "-i",
        str(audio),
        "-t",
        f"{duration:.3f}",
        "-vf",
        vf,
        "-af",
        f"apad=pad_dur={float(seg.get('tail_pad', 0.35))},aresample=48000",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        str(out),
    ], quiet=True)
    print("clip", out)
    return out


def demo_clip(cfg: dict[str, Any], seq: int, seg: dict[str, Any], *, force: bool = False) -> Path:
    paths = dirs(cfg)
    width, height, fps = int(cfg["width"]), int(cfg["height"]), int(cfg["fps"])
    src = Path(seg["path"])
    if not src.exists():
        raise FileNotFoundError(src)
    out = paths["clips"] / f"part_{seq:02d}_demo.mp4"
    if out.exists() and not force:
        return out
    vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    cmd = [ffmpeg(), "-y"]
    if seg.get("start") is not None:
        cmd += ["-ss", str(seg["start"])]
    cmd += ["-i", str(src)]
    if seg.get("duration") is not None:
        cmd += ["-t", str(seg["duration"])]
    cmd += [
        "-vf",
        vf,
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        str(out),
    ]
    run(cmd, quiet=True)
    print("demo", out)
    return out


def compose(cfg: dict[str, Any], *, force: bool = False) -> None:
    paths = dirs(cfg)
    parts: list[Path] = []
    for seq, seg in enumerate(cfg["segments"], start=1):
        kind = seg.get("type", "slide")
        if kind == "slide":
            parts.append(slide_clip(cfg, seq, seg, force=force))
        elif kind == "demo":
            parts.append(demo_clip(cfg, seq, seg, force=force))
        else:
            raise ValueError(f"unknown segment type: {kind}")

    concat = paths["work"] / "concat.txt"
    concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    final_tmp = paths["work"] / "final.mp4"
    run([ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(final_tmp)], quiet=True)

    output = Path(cfg["output_video"])
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(final_tmp, output)
    print("output", output)


def qc(cfg: dict[str, Any]) -> None:
    paths = dirs(cfg)
    target = Path(cfg["output_video"])
    if not target.exists():
        target = paths["work"] / "final.mp4"
    proc = subprocess.run([ffmpeg(), "-i", str(target)], capture_output=True, text=True, errors="ignore")
    for line in proc.stderr.splitlines():
        if "Duration" in line or "Stream #" in line:
            print(line.strip())
    if target.exists():
        print("size_mb", round(target.stat().st_size / 1024 / 1024, 2))
        for i, t in enumerate([3, 60, 180, 420, 700], start=1):
            out = paths["qc"] / f"qc_{i:02d}_{t:04d}.png"
            run([ffmpeg(), "-y", "-ss", str(t), "-i", str(target), "-frames:v", "1", str(out)], quiet=True)
            print("qc", out)


def write_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init-config", "shots", "voice", "compose", "qc", "all"])
    parser.add_argument("--config", default="course_video_config.json")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    if args.action == "init-config":
        write_default_config(config_path)
        return

    cfg = load_config(config_path)
    if args.action in {"shots", "all"}:
        render_slides(cfg, force=args.force)
    if args.action in {"voice", "all"}:
        generate_voice(cfg, force=args.force)
    if args.action in {"compose", "all"}:
        compose(cfg, force=args.force)
    if args.action in {"qc", "all"}:
        qc(cfg)


if __name__ == "__main__":
    main()
