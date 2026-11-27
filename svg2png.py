#!/usr/bin/env python3
"""Safe SVG -> crisp PNG renderer strictly confined to the project directory.

Uses macOS built-in QuickLook (qlmanage) for full WebKit rendering (Chinese fonts,
gradients, Apple Color Emoji) and Pillow to crop away qlmanage's square padding
according to the SVG's true aspect ratio.
"""

import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
OUTBOX_DIR = BASE_DIR / "_outbox"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _parse_num(val: Optional[str]) -> Optional[float]:
    if not val:
        return None
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", val)
    return float(m.group(1)) if m else None


def _svg_dimensions(svg_path: Path) -> Optional[Tuple[float, float]]:
    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
    except Exception:
        return None
    vb = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if vb:
        parts = re.split(r"[\s,]+", vb.strip())
        if len(parts) == 4:
            try:
                w, h = float(parts[2]), float(parts[3])
                if w > 0 and h > 0:
                    return w, h
            except ValueError:
                pass
    w = _parse_num(root.attrib.get("width"))
    h = _parse_num(root.attrib.get("height"))
    if w and h and w > 0 and h > 0:
        return w, h
    return None


def render_svg_to_png(svg_path: Path, out_png: Optional[Path] = None, max_size: int = 1600) -> Path:
    src = svg_path if svg_path.is_absolute() else (BASE_DIR / svg_path)
    src = src.resolve()
    if not _is_within(src, BASE_DIR):
        raise PermissionError(f"仅允许转换 {BASE_DIR} 目录内的 SVG 文件")
    if not src.exists() or src.suffix.lower() != ".svg":
        raise FileNotFoundError(f"找不到有效的 SVG 文件: {src}")

    if out_png is None:
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        dst = (OUTBOX_DIR / f"{src.stem}.png").resolve()
    else:
        dst = out_png if out_png.is_absolute() else (BASE_DIR / out_png)
        dst = dst.resolve()
        if not _is_within(dst, BASE_DIR):
            raise PermissionError(f"输出路径必须位于 {BASE_DIR} 目录内")
        dst.parent.mkdir(parents=True, exist_ok=True)

    dims = _svg_dimensions(src)
    with tempfile.TemporaryDirectory(prefix="svg2png_") as td:
        td_path = Path(td)
        proc = subprocess.run(
            ["/usr/bin/qlmanage", "-t", "-s", str(max_size), "-o", str(td_path), str(src)],
            capture_output=True,
            text=True,
        )
        raw_png = td_path / f"{src.name}.png"
        if proc.returncode != 0 or not raw_png.exists():
            raise RuntimeError(f"qlmanage 渲染失败: {proc.stderr.strip() or proc.stdout.strip()}")

        try:
            from PIL import Image

            with Image.open(raw_png) as img:
                img = img.convert("RGBA")
                iw, ih = img.size
                if dims:
                    sw, sh = dims
                    if sw >= sh:
                        target_h = max(1, min(ih, int(round(iw * sh / sw))))
                        cropped = img.crop((0, 0, iw, target_h))
                    else:
                        target_w = max(1, min(iw, int(round(ih * sw / sh))))
                        cropped = img.crop((0, 0, target_w, ih))
                else:
                    bbox = img.getbbox()
                    cropped = img.crop(bbox) if bbox else img
                cropped.save(dst, format="PNG")
        except Exception:
            shutil.copy2(raw_png, dst)

    return dst


def main() -> None:
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print("用法: ./svg2png.py <input.svg> [output.png]", file=sys.stderr)
        sys.exit(2)
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) == 3 else None
    out = render_svg_to_png(src, dst)
    print(str(out))


if __name__ == "__main__":
    main()
