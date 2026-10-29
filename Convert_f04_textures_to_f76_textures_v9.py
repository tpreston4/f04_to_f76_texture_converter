#!/usr/bin/env python3
# Version: 9.5

import os
import sys
import math
import struct
import json
import shutil
import subprocess
import tempfile
import threading
import concurrent.futures
from collections import namedtuple
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, colorchooser, simpledialog

# ------------------------------------------------------------------------------
# Check for Dependencies
# ------------------------------------------------------------------------------
def ensure_dependencies():
    missing = []
    try:
        from PIL import Image, ImageStat, ImageFilter, ImageOps, ImageTk, ImageChops, ImageDraw, ImageEnhance
    except ImportError:
        missing.append("Pillow>=10.0.0")

    try:
        import numpy
    except ImportError:
        missing.append("numpy")

    if missing:
        print(f"[FO4->76 Engine] Installing missing dependencies: {', '.join(missing)}...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing)
            print("[FO4->76 Engine] Packages successfully installed!\n")
        except Exception as e:
            print(f"[FO4->76 Engine] Error installing dependencies: {e}")
            sys.exit(1)

    # Pillow's DDS reader only gained reliable BC5/BC7 support in the 10.x
    # line. Older Pillow will silently mis-read FO4's _n/_s files.
    try:
        import PIL
        major = int(PIL.__version__.split(".")[0])
        if major < 10:
            print("[FO4->76 Engine] Upgrading Pillow for correct BC5/BC7 DDS reading...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "Pillow>=10.0.0"])
    except Exception as e:
        print(f"[FO4->76 Engine] Warning: could not verify/upgrade Pillow version: {e}")

ensure_dependencies()

import numpy as np
from PIL import Image, ImageStat, ImageFilter, ImageOps, ImageTk, ImageChops, ImageDraw, ImageEnhance


# Grab texconv from the environment path
TEXCONV_PATH = os.environ.get("TEXCONV_PATH", "texconv.exe")


# Check to make sure texconv is actually installed
def find_texconv() -> str:
    candidates = [
        TEXCONV_PATH,
        str(Path(__file__).resolve().parent / "texconv.exe"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    found = shutil.which("texconv.exe") or shutil.which("texconv")
    if found:
        return found
    raise FileNotFoundError(
        "texconv.exe was not found. Download it from "
        "https://github.com/microsoft/DirectXTex/releases, then place it "
        "next to this script, add it to your PATH, or set the TEXCONV_PATH "
        "environment variable to its full path."
    )


# Save the dds files
# We pass the format and type as a parameter
def save_dds(pil_img: "Image.Image", out_path: Path, dxgi_format: str,
             is_normal_map: bool = False, log_func=print) -> None:

    texconv = find_texconv()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # PNG keeps full 8-bit precision and texconv reads it natively.
        src_path = tmp_dir / (out_path.stem + ".png")
        pil_img.convert("RGB" if pil_img.mode not in ("RGBA",) else "RGBA").save(src_path)

        cmd = [texconv, "-f", dxgi_format, "-m", "0", "-y", "-o", str(tmp_dir)]

        if dxgi_format.endswith("_SRGB"):
            cmd += ["-srgb"]

        # normal maps require some extra arguments to save properly
        if is_normal_map:
            cmd += ["-nmap", "rg", "-nmapamp", "1.0"]
        cmd += [str(src_path)]

        result = subprocess.run(cmd, capture_output=True, text=True)
        produced = tmp_dir / (src_path.stem + ".dds")
        if result.returncode != 0 or not produced.exists():
            msg = (result.stdout or "") + (result.stderr or "")
            log_func(f"  ✗ texconv failed for {out_path.name}:\n{msg.strip()}")
            raise RuntimeError(f"texconv failed to produce {out_path.name}")

        shutil.copyfile(produced, out_path)


# ------------------------------------------------------------------------------
# Persistent app settings (custom color palette, output toggles, etc). Stored as
# plain JSON in the user's home directory so they survive closing/reopening the
# program - nothing here is texture data, just small UI preferences.
# V7 Update
# ------------------------------------------------------------------------------
SETTINGS_PATH = Path.home() / ".fo4_to_fo76_converter_settings.json"


def load_app_settings() -> dict:
    try:
        if SETTINGS_PATH.exists():
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[Warning] Could not load saved settings ({SETTINGS_PATH}): {e}")
    return {}


def save_app_settings(data: dict) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Warning] Could not save settings ({SETTINGS_PATH}): {e}")


# ------------------------------------------------------------------------------
# Material Presets Library: (Metalness/Reflectivity Bias, LightMult, SpecBoost, DefaultColor)
# Need to define this so we can have some base materials to go off of
# ------------------------------------------------------------------------------
# User-saved presets are shown in the dropdown alongside the built-ins below,
# prefixed with this so they're clearly distinguishable (and so a custom preset
# can never collide with/overwrite a built-in name).
CUSTOM_PRESET_PREFIX = "My: "

MATERIAL_PRESETS = {
    "Chrome":           (255, 1.0, 2.0, (220, 220, 225)),
    "Polished Steel":   (240, 1.0, 1.3, (180, 180, 185)),
    "Gold / Brass":     (250, 1.0, 1.5, (230, 180, 70)),
    "Copper":           (245, 1.0, 1.4, (215, 115, 80)),
    "Rusted / Dull":    (180, 0.9, 0.6, (140, 75, 50)),
    "Painted Metal":    (80,  1.0, 0.8, (60, 120, 190)),
    "Leather":          (15,  1.0, 0.5, (110, 65, 40)),
    "Cloth / Fabric":   (0,   1.0, 0.2, (150, 150, 150)),
    "Dull Unpolished":  (120, 0.9, 0.5, (100, 100, 105))
}

# ------------------------------------------------------------------------------
# FO4 .bgsm material reader
#
# A .bgsm is the material file FO4 meshes point at. It's where the environment map
# (cubemap) lives - the textures themselves (_d/_n/_s) never contain it. Layout
# below is for BGSM version 2 (what FO4 uses), checked against real sample files:
# the parse has to consume the file exactly and land on sane values, otherwise the
# file is rejected rather than half-trusted. Other versions have different layouts
# (FO76's, for example), so they're skipped with a clear message, not guessed at.
# ------------------------------------------------------------------------------
BgsmInfo = namedtuple("BgsmInfo", [
    "version", "env_mapping", "env_mask_scale", "diffuse", "normal", "spec", "cubemap",
    "specular_enabled", "specular_color", "specular_mult", "smoothness", "fresnel_power",
    "header",
])


class _BgsmReader:
    def __init__(self, data: bytes, pos: int = 0):
        self.d = data
        self.p = pos

    def _take(self, fmt: str):
        size = struct.calcsize(fmt)
        if self.p + size > len(self.d):
            raise ValueError("unexpected end of file")
        v = struct.unpack_from(fmt, self.d, self.p)
        self.p += size
        return v[0] if len(v) == 1 else v

    def u8(self):
        return self._take("<B")

    def u32(self):
        return self._take("<I")

    def f32(self):
        return self._take("<f")

    def skip(self, n: int):
        if self.p + n > len(self.d):
            raise ValueError("unexpected end of file")
        self.p += n

    def string(self) -> str:
        n = self.u32()          # length includes the trailing NUL
        if n > 1024 or self.p + n > len(self.d):
            raise ValueError("bad string length")
        raw = self.d[self.p:self.p + n]
        self.p += n
        return raw.rstrip(b"\0").decode("latin-1")


def read_bgsm(path) -> "BgsmInfo":
    data = Path(path).read_bytes()
    if data[:4] != b"BGSM":
        raise ValueError("not a BGSM file")
    version = struct.unpack_from("<I", data, 4)[0]
    if version != 2:
        raise ValueError(f"BGSM version {version} isn't supported (only version 2, used by Fallout 4)")

    r = _BgsmReader(data, 8)
    # This part of the header is laid out identically in FO4 (v2) and FO76 (v22) files,
    # which is what lets a converted material inherit it.
    header = {}
    header["tile"] = r.u32()
    header["uv"] = (r.f32(), r.f32(), r.f32(), r.f32())     # U/V offset, U/V scale
    header["alpha"] = r.f32()
    header["blend"] = (r.u8(), r.u32(), r.u32())            # mode, source, destination
    header["flags"] = tuple(r.u8() for _ in range(12))      # see FO76_HEADER_FLAG_NAMES
    header["refraction_power"] = r.f32()
    env_mapping = bool(r.u8())
    env_mask_scale = r.f32()
    r.u8()                        # grayscale to palette color

    diffuse, normal, spec, _greyscale = r.string(), r.string(), r.string(), r.string()
    cubemap = r.string()
    for _ in range(4):            # glow, inner layer, wrinkles, displacement
        r.string()

    r.u8()                        # enable editor alpha ref
    r.u8(); r.f32(); r.f32()      # rim lighting, rim power, back light power
    r.u8(); r.f32()               # subsurface lighting, rolloff
    specular_enabled = bool(r.u8())
    specular_color = (r.f32(), r.f32(), r.f32())
    specular_mult = r.f32()
    smoothness = r.f32()
    fresnel_power = r.f32()
    r.skip(6 * 4)                 # wetness control: spec scale, spec power scale, spec min var,
                                  # env map scale, fresnel power, metalness
    root_path = r.string()

    # Sanity checks: a wrong layout would drift into garbage by here.
    if not (0.0 <= env_mask_scale <= 100.0) or not (0.0 <= smoothness <= 10.0):
        raise ValueError("values out of range - layout doesn't match")
    if root_path and not root_path.lower().endswith(".bgsm"):
        raise ValueError("layout doesn't match (root material path)")

    return BgsmInfo(version, env_mapping, env_mask_scale, diffuse, normal, spec, cubemap,
                    specular_enabled, specular_color, specular_mult, smoothness, fresnel_power,
                    header)


def _norm_tex_path(p: str) -> str:
    p = (p or "").replace("\\", "/").strip().lstrip("/").lower()
    if p.startswith("textures/"):
        p = p[len("textures/"):]
    return p


def _bgsm_set_keys(info: "BgsmInfo") -> set:
    # Check for different naming conventions.
    # We also try to search based on the bgsm file as well, but this is a fallback
    variants = (
        (info.diffuse, ("_d", "d", "")),
        (info.normal, ("_n", "n")),
        (info.spec, ("_s", "s", "_r", "r")),
    )
    counts = {}
    for tex, suffixes in variants:
        t = _norm_tex_path(tex)
        if not t.endswith(".dds"):
            continue
        stem = t[:-4]
        seen = set()
        for suf in suffixes:
            if suf and not stem.endswith(suf):
                continue
            base = stem[:len(stem) - len(suf)] if suf else stem
            # a bare letter suffix ("d") must not leave a dangling underscore behind
            if suf and not suf.startswith("_") and base.endswith("_"):
                continue
            if base and not base.endswith("/"):
                seen.add(base)
        for base in seen:
            counts[base] = counts.get(base, 0) + 1
    if not counts:
        return set()
    best = max(counts.values())
    return {b for b, c in counts.items() if c == best}


def load_bgsm_materials(materials_dir: Path):
    # Every readable .bgsm under materials_dir (all subfolders) as [(BgsmInfo, path)], plus
    # a list of "file: reason" strings for the ones that were skipped.
    materials, errors = [], []
    for root, dirs, files in os.walk(materials_dir):
        dirs.sort()
        for fn in sorted(files):
            if not fn.lower().endswith(".bgsm"):
                continue
            fp = Path(root) / fn
            try:
                materials.append((read_bgsm(fp), fp))
            except Exception as e:
                errors.append(f"{fn}: {e}")
    return materials, errors


def index_bgsm_materials(materials: list) -> dict:
    # base-name -> [(base_key, BgsmInfo, bgsm_path)]
    index = {}
    for info, fp in materials:
        for key in _bgsm_set_keys(info):
            index.setdefault(key.rsplit("/", 1)[-1], []).append((key, info, fp))
    return index


def build_bgsm_index(materials_dir: Path):
    materials, errors = load_bgsm_materials(materials_dir)
    return index_bgsm_materials(materials), len(materials), errors


# --- Finding texture sets from the materials themselves (instead of *_d.dds naming) ---
def build_dds_index(in_dir: Path) -> dict:
    idx = {}
    for root, _dirs, files in os.walk(in_dir):
        for fn in files:
            if fn.lower().endswith(".dds"):
                idx.setdefault(fn.lower(), []).append(Path(root) / fn)
    return idx


def locate_texture(dds_index: dict, tex_path: str):
    # Finds the file a material's texture path refers to. Returns (Path or None, note).
    # Same rule as material matching: the candidate sharing the most trailing folders wins,
    # so it works whether or not Input Dir keeps the folders the material names.
    t = _norm_tex_path(tex_path)
    if not t.endswith(".dds"):
        return None, "not listed"
    parts = t.split("/")
    cands = dds_index.get(parts[-1], [])
    if not cands:
        return None, "not found in Input Dir"
    scored = []
    for c in cands:
        cparts = os.path.abspath(str(c)).replace("\\", "/").lower().split("/")
        n = 0
        while n < min(len(parts), len(cparts)) and parts[-1 - n] == cparts[-1 - n]:
            n += 1
        scored.append((n, c))
    best = max(n for n, _c in scored)
    top = [c for n, c in scored if n == best]
    if len(top) > 1:
        return None, "several files with that name in different folders"
    return top[0], ("" if best == len(parts) else "matched by file name only")


def discover_texture_sets(materials: list, in_dir: Path, materials_root: Path = None):
    # One texture set per material whose diffuse texture exists in in_dir. Returns
    # (sets, skipped): sets are dicts (key, diffuse, normal, spec, info, fp); skipped are
    # "file: reason" strings for materials whose diffuse couldn't be found.
    dds_index = build_dds_index(in_dir)
    sets, skipped = [], []
    for info, fp in materials:
        diffuse, note = locate_texture(dds_index, info.diffuse)
        if diffuse is None:
            skipped.append(f"{fp.name}: diffuse '{info.diffuse}' {note}")
            continue
        normal, _n = locate_texture(dds_index, info.normal)
        spec, _s = locate_texture(dds_index, info.spec)
        try:
            key = fp.relative_to(materials_root).with_suffix("").as_posix()
        except Exception:
            key = fp.stem
        sets.append(dict(key=key, diffuse=diffuse, normal=normal, spec=spec, info=info, fp=fp))
    sets.sort(key=lambda d: d["key"].lower())
    return sets, skipped


def find_bgsm_matches(index: dict, diffuse_file: Path):
    # Returns (matches, note). A material's texture paths (e.g. DX/RedRibbon/Heels) are
    # compared with the set's real path from the end, and the candidate(s) sharing the most
    # trailing folders win - so it still matches when the input folder is flat or renamed,
    # as long as the file name is unambiguous.
    full = os.path.abspath(str(diffuse_file)).replace("\\", "/").lower()
    if full.endswith("_d.dds"):
        full = full[:-6]
    else:
        full = full.rsplit(".", 1)[0]
    parts = full.split("/")
    last = parts[-1]

    scored = []
    for key, info, fp in index.get(last, []):
        kparts = key.split("/")
        n = 0
        while n < min(len(parts), len(kparts)) and parts[-1 - n] == kparts[-1 - n]:
            n += 1
        scored.append((n, key, info, fp))
    if not scored:
        return [], f"no .bgsm references a texture set named '{last}'"

    best = max(c[0] for c in scored)
    top = [c for c in scored if c[0] == best]
    if len({c[1] for c in top}) > 1:
        dirs = ", ".join(sorted({c[1] for c in top})[:3])
        return [], (f"several materials use a texture named '{last}' in different folders ({dirs}); "
                    f"can't tell which applies")
    found = {str(c[3]): (c[2], c[3]) for c in top}
    matches = [found[k] for k in sorted(found)]
    note = ""
    if best < len(top[0][1].split("/")):
        note = "matched by file name only (folder layout differs from the .bgsm)"
    return matches, note


# ------------------------------------------------------------------------------
# FO76 .bgsm writer
#
# Still a WIP, but seems to work as expected for simple bgsm files
# You may need to fix them after they're created
# ------------------------------------------------------------------------------
FO76_BGSM_VERSION = 22

# Order of the 12 single-byte flags in the header.
FO76_HEADER_FLAG_NAMES = (
    "alpha_test_ref", "alpha_test", "z_write", "z_test", "screen_space_reflections",
    "wetness_ssr", "decal", "two_sided", "decal_no_fade", "non_occluder",
    "refraction", "refraction_falloff",
)

# Header values taken from the FO76 example files.
FO76_DEFAULT_HEADER = {
    "tile": 3,
    "uv": (0.0, 0.0, 1.0, 1.0),
    "alpha": 1.0,
    "blend": (0, 6, 7),
    "flags": (128, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0),
    "refraction_power": 0.0,
}
# Remaining header bytes the examples share: depth bias, grayscale-to-palette, mask writes.
_FO76_HEADER_TRAILER = bytes([0x00, 0x00, 0x3F])

# Everything after the 10 texture slots.
_FO76_BGSM_TAIL = bytes.fromhex(
    "00 00 00 01 00 00 80 3f 00 00 80 3f 00 00 80 3f 00 00 80 3f 00 00 00 3f 01 00 00 80 3f "
    "00 00 80 3f 00 00 80 3f 00 00 80 3f 00 00 80 3f 00 00 a0 40 00 00 80 bf 00 00 80 bf "
    "00 00 80 bf 00 00 80 bf 00 00 80 bf 01 00 00 00 00 00 01 00 00 00 00 00 00 00 00 80 3f "
    "00 00 00 00 c8 42 00 00 00 00 00 00 00 00 00 00 00 80 3f 00 00 01 00 00 00 00 81 80 "
    "00 3f 81 80 00 3f 81 80 00 3f 00 00 00 00 81 80 00 3f 00 00"
)


def _bgsm_string(text: str) -> bytes:
    raw = (text or "").encode("latin-1", errors="replace") + b"\0"
    return struct.pack("<I", len(raw)) + raw


def build_fo76_bgsm(diffuse: str = "", normal: str = "", reflection: str = "", lightmap: str = "",
                    header: dict = None) -> bytes:
    # Texture arguments are game-relative paths (e.g. "ribbon/heels/Heels_d.dds"); an empty
    # string leaves that slot unused. `header` (same shape as FO76_DEFAULT_HEADER, as read from
    # a material by read_bgsm) overrides the defaults.
    h = dict(FO76_DEFAULT_HEADER)
    if header:
        h.update({k: header[k] for k in FO76_DEFAULT_HEADER if k in header})

    out = bytearray(b"BGSM")
    out += struct.pack("<I", FO76_BGSM_VERSION)
    out += struct.pack("<I", h["tile"])
    out += struct.pack("<4f", *h["uv"])
    out += struct.pack("<f", h["alpha"])
    out += struct.pack("<BII", *h["blend"])
    out += struct.pack("<12B", *h["flags"])
    out += struct.pack("<f", h["refraction_power"])
    out += _FO76_HEADER_TRAILER

    # Texture slots: diffuse, normal, smooth-spec, greyscale, glow, wrinkles, specular (_r),
    # lighting (_l), flow, distance-field alpha.
    for tex in (diffuse, normal, "", "", "", "", reflection, lightmap, "", ""):
        out += _bgsm_string(tex)

    out += _FO76_BGSM_TAIL
    return bytes(out)


def fo76_game_path(out_file: Path, tex_root: Path) -> str:
    # The path a material uses for a texture: everything after the last "Textures" folder in
    # the real path if there is one, otherwise the path relative to the output folder.
    parts = list(Path(os.path.abspath(str(out_file))).parts)
    lowered = [x.lower() for x in parts]
    if "textures" in lowered:
        i = len(lowered) - 1 - lowered[::-1].index("textures")
        rel = parts[i + 1:]
    else:
        rel = list(Path(os.path.abspath(str(out_file))).relative_to(
            Path(os.path.abspath(str(tex_root)))).parts)
    return "/".join(rel)


def write_fo76_bgsm(jobs: list, material_dir: Path, tex_root: Path, header: dict = None) -> Path:
    # One .bgsm per texture set, pointing at whichever of _d/_n/_r/_l were generated.
    slots = {"_d.dds": "diffuse", "_n.dds": "normal", "_r.dds": "reflection", "_l.dds": "lightmap"}
    paths, base = {}, None
    for job in jobs:
        name = job.out_path.name
        for suffix, slot in slots.items():
            if name.lower().endswith(suffix):
                paths[slot] = fo76_game_path(job.out_path, tex_root)
                base = base or name[:-len(suffix)]
    if not paths or base is None:
        raise ValueError("no texture outputs to reference")
    material_dir = Path(material_dir)
    material_dir.mkdir(parents=True, exist_ok=True)
    out_path = material_dir / f"{base}.bgsm"
    out_path.write_bytes(build_fo76_bgsm(header=header, **paths))
    return out_path


# Cubemap file-name keywords -> built-in preset. Order matters: the first hit wins, so the
# specific names come before the generic "metal". This is a name-based heuristic - the
# .bgsm only stores a path to the cubemap, not what material it represents.
# reminder to self, add more if you find more
CUBEMAP_KEYWORDS = [
    (("chrome",), "Chrome"),
    (("gold", "brass"), "Gold / Brass"),
    (("copper", "bronze"), "Copper"),
    (("rust",), "Rusted / Dull"),
    (("steel", "iron", "silver"), "Polished Steel"),
    (("leather",), "Leather"),
    (("cloth", "fabric", "cotton"), "Cloth / Fabric"),
    (("metal",), "Polished Steel"),
]
# Presets whose reflections are visibly colored (not neutral), so the reflection map gets
# tinted with the preset's color.
# reminder to self, add more if you find more/need more added
TINTED_PRESETS = {"Gold / Brass", "Copper"}


def classify_bgsm_material(info: "BgsmInfo"):
    # Returns (preset_name or None, reason).
    if not info.env_mapping or not info.cubemap.strip():
        return "Cloth / Fabric", "no environment cubemap, so nothing to reflect"
    name = Path(info.cubemap.replace("\\", "/")).stem.lower()
    for words, preset in CUBEMAP_KEYWORDS:
        if any(w in name for w in words):
            return preset, f"cubemap '{Path(info.cubemap.replace(chr(92), '/')).name}'"
    return None, f"cubemap '{Path(info.cubemap.replace(chr(92), '/')).name}' isn't recognised"


def material_overrides_from_bgsm(info: "BgsmInfo"):
    # Turns a material file into pipeline-parameter overrides (the same values choosing
    # a preset would set), plus a human-readable description for the log/label.
    preset, reason = classify_bgsm_material(info)
    if preset is None:
        return {}, f"{reason} - using the manual sliders"
    metal, light, spec, color = MATERIAL_PRESETS[preset]
    scale = min(3.0, max(0.1, info.env_mask_scale))
    overrides = dict(
        metal_offset=int(metal) - 180,
        light_mult=float(light),
        reflection_boost=min(3.0, max(0.1, spec * scale)),
        # The cubemap says the material *can* reflect; the spec map says *where*.
        # Gate the bias per pixel so e.g. leather on a chrome-cubemap material
        # doesn't get blanket reflectivity.
        gate_bias_by_spec=True,
    )
    if preset in TINTED_PRESETS:
        overrides["tint_rgb"] = tuple(color)
    return overrides, f"{reason} -> {preset}"


def resolve_material_for_info(info: "BgsmInfo", fp: Path):
    # Same as resolve_material_for_set, for when the material is already known.
    overrides, desc = material_overrides_from_bgsm(info)
    return overrides, f"{fp.name}: {desc}"


def resolve_material_for_set(index: dict, diffuse_file: Path):
    # (overrides, message) for one texture set, looked up through the materials index.
    if not index:
        return {}, "no materials loaded"
    matches, note = find_bgsm_matches(index, diffuse_file)
    if not matches:
        return {}, f"{note} - using the manual sliders"
    info, fp = matches[0]
    overrides, desc = material_overrides_from_bgsm(info)
    msg = f"{fp.name}: {desc}"
    if note:
        msg += f"  ({note})"
    if len(matches) > 1:
        others = ", ".join(m[1].name for m in matches[1:])
        msg += (f"  [!] {len(matches)} materials share these textures (also {others}); "
                f"used {fp.name}")
    return overrides, msg


# Used to auto-suggest a default brush color when you pick an input folder
def get_average_color(image_path: Path) -> tuple:
    try:
        if image_path and image_path.exists():
            diffuse_img = Image.open(image_path)
            img_rgb = diffuse_img.convert("RGB")
            small = img_rgb.resize((32, 32), Image.Resampling.BOX)
            stat = ImageStat.Stat(small)
            mean = stat.mean
            return (int(mean[0]), int(mean[1]), int(mean[2]))
    except Exception:
        pass
    return (180, 180, 185)


# ------------------------------------------------------------------------------
# Re-adding the PBR sphere
# ------------------------------------------------------------------------------
class PBRSphereRenderer:
    def __init__(self, width=220, height=220):
        self.width = width
        self.height = height
        self.center_x = width // 2
        self.center_y = height // 2
        self.radius = min(width, height) // 2 - 12

        lx, ly, lz = 0.5, 0.7, 0.8
        l_len = math.sqrt(lx * lx + ly * ly + lz * lz)
        self.light_dir = (lx / l_len, ly / l_len, lz / l_len)

    def render(self, smoothness: float, metalness: float, base_color: tuple,
               light_mult: float, spec_boost: float) -> "Image.Image":
        img = Image.new("RGB", (self.width, self.height), (24, 25, 28))
        pixels = img.load()

        lx, ly, lz = self.light_dir
        r_r, g_r, b_r = base_color

        smooth = smoothness / 255.0
        metal = metalness / 255.0
        roughness = max(0.02, 1.0 - smooth)
        shininess = max(2.0, (1.0 - roughness) * 128.0)

        sky_col = (180, 210, 245)
        ground_col = (45, 40, 35)

        for y in range(self.height):
            dy = y - self.center_y
            for x in range(self.width):
                dx = x - self.center_x
                dist_sq = dx * dx + dy * dy

                if dist_sq <= self.radius * self.radius:
                    nz = math.sqrt(max(0.0, self.radius * self.radius - dist_sq))
                    nx = dx / self.radius
                    ny = dy / self.radius
                    nz = nz / self.radius

                    ndotl = max(0.0, nx * lx + ny * ly + nz * lz)

                    rx = 2 * nz * nx
                    ry = 2 * nz * ny
                    rz = 2 * nz * nz - 1.0

                    env_factor = max(0.0, ry)
                    env_r = sky_col[0] * env_factor + ground_col[0] * (1.0 - env_factor)
                    env_g = sky_col[1] * env_factor + ground_col[1] * (1.0 - env_factor)
                    env_b = sky_col[2] * env_factor + ground_col[2] * (1.0 - env_factor)

                    spec_power = math.pow(max(0.0, rz), shininess) * spec_boost

                    diffuse_factor = (1.0 - metal) * light_mult

                    spec_tint_r = (1.0 - metal) + metal * (r_r / 255.0)
                    spec_tint_g = (1.0 - metal) + metal * (g_r / 255.0)
                    spec_tint_b = (1.0 - metal) + metal * (b_r / 255.0)

                    out_r = (r_r * ndotl * diffuse_factor) + (env_r * metal * smooth * (r_r / 255.0)) + (spec_power * 255 * spec_tint_r)
                    out_g = (g_r * ndotl * diffuse_factor) + (env_g * metal * smooth * (g_r / 255.0)) + (spec_power * 255 * spec_tint_g)
                    out_b = (b_r * ndotl * diffuse_factor) + (env_b * metal * smooth * (b_r / 255.0)) + (spec_power * 255 * spec_tint_b)

                    fresnel = math.pow(1.0 - max(0.0, nz), 3.0) * (0.2 + 0.8 * smooth)
                    out_r += sky_col[0] * fresnel
                    out_g += sky_col[1] * fresnel
                    out_b += sky_col[2] * fresnel

                    final_r = int(min(255, max(0, out_r)))
                    final_g = int(min(255, max(0, out_g)))
                    final_b = int(min(255, max(0, out_b)))

                    pixels[x, y] = (final_r, final_g, final_b)

        return img


# ------------------------------------------------------------------------------
# FO76 Texture Channel Packing Helpers
# ------------------------------------------------------------------------------

# This one does the normal map conversion
# We're changing the format from the original, so we need to update the green channel
# Changed to help with the texture preview
def process_fo76_normal_map_from_image(img_n: "Image.Image") -> "Image.Image":
    img_n = img_n.convert("RGBA")

    # DirectX Normal Conversion
    r_chan = img_n.getchannel("R")
    g_chan = Image.eval(img_n.getchannel("G"), lambda x: 255 - x)  # Flip Y axis for FO76
    b_placeholder = Image.new("L", img_n.size, 128)

    return Image.merge("RGB", (r_chan, g_chan, b_placeholder))


def process_fo76_normal_map(norm_path: Path) -> "Image.Image":
    return process_fo76_normal_map_from_image(Image.open(norm_path))


# Building the reflection map
# Requires us to build it via the diffuse map and the specular map
# Need to make sure they're all the same size and correct if not
# Also need to take the adjustments into account
# We now need to "cache" images to keep things responsize, so need to update the logic here
#
# FO4's material files store per-pixel "Environment Mapping" (cubemap reflection)
# strength in the alpha channel of the specular map (_s.dds) - 0 = that pixel never
# reflects the environment, 255 = it fully does. FO76's _r.dds is the direct
# equivalent of that system, so we carry the mask over: any part of the item FO4
# marked as non-reflective (leather straps on a metal breastplate, skin next to a
# visor, etc.) is kept non-reflective here too, instead of inheriting whatever
# reflectivity the rest of the material has.
def process_fo76_reflection_map(
    diffuse_img: Image.Image,
    spec_path: Path,
    target_size,
    reflection_strength: float = 1.6,
    contrast: float = 1.35,
    bias: float = 0.0,
    spec_img: "Image.Image" = None,
    use_env_mask: bool = False,
    use_spec_tint: bool = False,
    tint_rgb: tuple = None,
    gate_bias_by_spec: bool = False,
) -> "Image.Image":

    if diffuse_img.size != target_size:
        diffuse_img = diffuse_img.resize(target_size, Image.Resampling.LANCZOS)
    diffuse_rgb = diffuse_img.convert("RGB")
    diffuse_arr = np.asarray(diffuse_rgb, dtype=np.float32) / 255.0
    diffuse_luma = (0.299 * diffuse_arr[:, :, 0] +
                    0.587 * diffuse_arr[:, :, 1] +
                    0.114 * diffuse_arr[:, :, 2])

    img_s = None
    if spec_img is not None:
        img_s = spec_img
        if img_s.size != target_size:
            img_s = img_s.resize(target_size, Image.Resampling.LANCZOS)
    elif spec_path and spec_path.exists():
        img_s = Image.open(spec_path)
        if img_s.size != target_size:
            img_s = img_s.resize(target_size, Image.Resampling.LANCZOS)

    spec_arr = None
    if img_s is not None:
        img_s = img_s.convert("RGBA")
        spec_arr = np.asarray(img_s, dtype=np.float32) / 255.0
        spec_red = spec_arr[:, :, 0]
    else:
        spec_red = diffuse_luma.copy()

    delta = spec_red - diffuse_luma
    reflect = diffuse_luma + delta * reflection_strength
    reflect = 0.5 + (reflect - 0.5) * contrast
    # Normally the bias lifts every pixel equally. When it comes from a material file's
    # cubemap (gate_bias_by_spec), it's scaled by the spec map instead, so only areas
    # the spec map marks as reflective receive it.
    reflect = reflect + (bias * spec_red if gate_bias_by_spec else bias)
    reflect = np.clip(reflect, 0.0, 1.0)

    # Apply the FO4 environment mask (spec alpha) as a final per-pixel multiplier.
    # Note: PIL fills in a fully-opaque (255) alpha channel for spec maps that
    # don't actually carry one, so sets without an embedded env mask behave
    # exactly as before (multiplying by 1.0 everywhere).
    if use_env_mask and spec_arr is not None:
        env_mask = spec_arr[:, :, 3]
        reflect = reflect * env_mask

    # In FO4, the cubemap's sampled environment color is multiplied by the
    # specular map's own RGB before it reaches the screen - a gold, copper, or
    # brass specular tint colors the reflection rather than leaving it grayscale.
    # Reproduce that: pull the hue/saturation out of the spec map (with its own
    # luma factored out, since strength is already handled above) and apply it
    # as a per-channel tint over the achromatic reflectivity we just computed.
    if tint_rgb is not None:
        # Material-level tint (e.g. gold/copper cubemap), constant across the texture.
        t = np.asarray(tint_rgb, dtype=np.float32) / 255.0
        t_luma = max(0.02, float(0.299 * t[0] + 0.587 * t[1] + 0.114 * t[2]))
        tint = np.clip(t / t_luma, 0.0, 3.0)
        reflect_rgb = np.clip(reflect[:, :, None] * tint[None, None, :], 0.0, 1.0)
    elif use_spec_tint and spec_arr is not None:
        spec_rgb = spec_arr[:, :, :3]
        spec_luma = (0.299 * spec_rgb[:, :, 0] +
                     0.587 * spec_rgb[:, :, 1] +
                     0.114 * spec_rgb[:, :, 2])
        spec_luma_safe = np.clip(spec_luma, 0.02, 1.0)[:, :, None]
        tint = np.clip(spec_rgb / spec_luma_safe, 0.0, 3.0)
        reflect_rgb = np.clip(reflect[:, :, None] * tint, 0.0, 1.0)
    else:
        reflect_rgb = np.repeat(reflect[:, :, None], 3, axis=2)

    reflect_8 = np.clip(reflect_rgb * 255.0, 0, 255).astype(np.uint8)
    reflect_img = Image.fromarray(reflect_8, mode="RGB")

    return reflect_img


# Create the AO here using the normal and diffuse map
# We can't "bake" a map using python as far as I know, so we need to simulate it here
# Since we added the preview, we need to "cache" the normal map
def bake_ao_from_normal_and_diffuse(
    normal_path: Path,
    diffuse_img: "Image.Image",
    target_size,
    ao_strength: float = 1.4,
    blur_radius: float = 3.0,
    diffuse_weight: float = 0.35,
    normal_img: "Image.Image" = None,
) -> "Image.Image":

    # Normal-based cavity term
    img_n = None
    if normal_img is not None:
        img_n = normal_img
        if img_n.size != target_size:
            img_n = img_n.resize(target_size, Image.Resampling.LANCZOS)
    elif normal_path and Path(normal_path).exists():
        img_n = Image.open(normal_path).convert("RGB")
        if img_n.size != target_size:
            img_n = img_n.resize(target_size, Image.Resampling.LANCZOS)

    if img_n is not None:
        n_arr = np.asarray(img_n, dtype=np.float32) / 255.0
        nx = n_arr[:, :, 0] * 2.0 - 1.0
        ny = n_arr[:, :, 1] * 2.0 - 1.0
        divergence = np.gradient(nx, axis=1) + np.gradient(ny, axis=0)
        cavity = -divergence
        cavity = cavity - float(np.mean(cavity))
        normal_term = 1.0 - np.clip(cavity * ao_strength, 0.0, 1.0)
    else:
        normal_term = np.ones((target_size[1], target_size[0]), dtype=np.float32)

    # Diffuse-based term: darker-than-neighborhood detection
    if diffuse_img is not None:
        diff_gray = diffuse_img.convert("L")
        if diff_gray.size != target_size:
            diff_gray = diff_gray.resize(target_size, Image.Resampling.LANCZOS)
        diff_arr = np.asarray(diff_gray, dtype=np.float32) / 255.0
        local_avg = np.asarray(
            diff_gray.filter(ImageFilter.GaussianBlur(radius=max(4.0, blur_radius * 4))),
            dtype=np.float32,
        ) / 255.0
        local_avg_safe = np.clip(local_avg, 0.05, 1.0)
        # Ratio < 1 wherever a pixel is darker than its local neighborhood.
        diffuse_term = np.clip(diff_arr / local_avg_safe, 0.0, 1.0)
    else:
        diffuse_term = normal_term
        diffuse_weight = 0.0

    combined = (1.0 - diffuse_weight) * normal_term + diffuse_weight * diffuse_term
    combined = np.clip(combined, 0.0, 1.0)

    ao_img = Image.fromarray((combined * 255.0).astype(np.uint8), mode="L")
    if blur_radius > 0:
        ao_img = ao_img.filter(ImageFilter.GaussianBlur(blur_radius))

    return ao_img


# Here, we build the light map
# We use the Specular file from FO4 and the AO map we create above
def process_fo76_lightmap(
    normal_path: Path,
    spec_path: Path,
    diffuse_img: "Image.Image",
    target_size,
    use_white_ao: bool = True,
    ao_strength: float = 1.4,
    ao_blur: float = 3.0,
    sss_strength: int = 0
) -> "Image.Image":

    if spec_path and spec_path.exists():
        img_s = Image.open(spec_path).convert("RGBA")
        if img_s.size != target_size:
            img_s = img_s.resize(target_size, Image.Resampling.LANCZOS)
        spec_chan = img_s.getchannel("G")
    else:
        spec_chan = Image.new("L", target_size, 128)

    # If we want a white AO map via the checkbox, we create it here
    if use_white_ao:
        ao_chan = Image.new("L", target_size, 255)
    else:
        ao_chan = bake_ao_from_normal_and_diffuse(
            normal_path, diffuse_img, target_size, ao_strength, ao_blur
        )

    # Build the SSS channel from the input, usually black
    sss_val = int(min(255, max(0, sss_strength)))
    sss_chan = Image.new("L", target_size, sss_val)

    return Image.merge("RGB", (spec_chan, ao_chan, sss_chan))

# Begin main v6 changes
# Other changes above affected by below changes
# Remove the PBR preview and replaced with texture preview
# ------------------------------------------------------------------------------
# Builds the 4 FO76 texture images in-memory (no disk writes). Used by both the
# live preview panel and the final export pipeline, so what you see is what
# you get.
# ------------------------------------------------------------------------------
def build_fo76_images(diffuse_path: Path, normal_path: Path, spec_path: Path,
                       metal_offset: int, light_mult: float,
                       reflection_boost: float = 1.6, use_white_ao: bool = True,
                       ao_strength: float = 1.4, sss_strength: int = 0,
                       use_env_mask: bool = False,
                       use_spec_tint: bool = False,
                       tint_rgb: tuple = None,
                       gate_bias_by_spec: bool = False,
                       enabled_maps: set = None):

    if enabled_maps is None:
        enabled_maps = {"diffuse", "normal", "reflection", "lightmap"}

    # Reflection always needs the diffuse source; lightmap only needs it when
    # baking AO from scratch (use_white_ao off) rather than using a flat white AO.
    need_diffuse_src = bool(
        enabled_maps & {"diffuse", "reflection"} or
        ("lightmap" in enabled_maps and not use_white_ao)
    )

    images = {}
    img_d_raw = Image.open(diffuse_path) if (need_diffuse_src and diffuse_path and diffuse_path.exists()) else None

    if img_d_raw is not None:
        target_size = img_d_raw.size
    elif normal_path and normal_path.exists():
        with Image.open(normal_path) as tmp_n:
            target_size = tmp_n.size
    else:
        target_size = (2048, 2048)

    has_alpha = False
    original_diffuse_img = None

    if img_d_raw is not None:
        has_alpha = img_d_raw.mode in ("RGBA", "LA") or (img_d_raw.mode == "P" and "transparency" in img_d_raw.info)
        original_diffuse_img = img_d_raw.convert("RGBA")

        if "diffuse" in enabled_maps:
            img_d = original_diffuse_img.copy()
            if light_mult != 1.0:
                r, g, b, a = img_d.split()
                rgb = Image.merge("RGB", (r, g, b))
                rgb = Image.eval(rgb, lambda x: int(min(255, x * light_mult)))
                r, g, b = rgb.split()
                img_d = Image.merge("RGBA", (r, g, b, a))

            if not has_alpha:
                r, g, b, _ = img_d.split()
                img_d = Image.merge("RGBA", (r, g, b, Image.new("L", target_size, 255)))

            images["diffuse"] = img_d

    if "normal" in enabled_maps and normal_path and normal_path.exists():
        img_n_rgb = process_fo76_normal_map(normal_path)
        images["normal"] = Image.merge(
            "RGBA", (*img_n_rgb.split(), Image.new("L", img_n_rgb.size, 255))
        )

    if "reflection" in enabled_maps and original_diffuse_img is not None:
        reflectivity_bias = metal_offset / 255.0
        img_r_rgb = process_fo76_reflection_map(
            original_diffuse_img, spec_path, target_size,
            reflection_strength=reflection_boost, bias=reflectivity_bias,
            use_env_mask=use_env_mask, use_spec_tint=use_spec_tint,
            tint_rgb=tint_rgb, gate_bias_by_spec=gate_bias_by_spec
        )
        images["reflection"] = Image.merge(
            "RGBA", (*img_r_rgb.split(), Image.new("L", img_r_rgb.size, 255))
        )

    if "lightmap" in enabled_maps:
        img_l_rgb = process_fo76_lightmap(
            normal_path, spec_path, original_diffuse_img, target_size,
            use_white_ao=use_white_ao, ao_strength=ao_strength, sss_strength=sss_strength
        )
        images["lightmap"] = Image.merge(
            "RGBA", (*img_l_rgb.split(), Image.new("L", img_l_rgb.size, 255))
        )

    return images, has_alpha, target_size


MAP_LABELS = {
    "diffuse": "Diffuse (_d)",
    "normal": "Normal (_n)",
    "reflection": "Reflection (_r)",
    "lightmap": "Lightmap (_l)",
}

# Working resolution cap for the live preview panel only. Export (Convert Textures)
# and the per-texture editor always use the full source resolution - this cap only
# limits the cost of the little 140x140 thumbnails so slider drags stay responsive
# even on 2048/4096px source textures.
PREVIEW_RES = 384


def _downscale(img: "Image.Image", max_dim: int) -> "Image.Image":
    img.load()
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    ratio = max_dim / max(w, h)
    new_size = (max(1, int(w * ratio)), max(1, int(h * ratio)))
    return img.resize(new_size, Image.Resampling.LANCZOS)


# ------------------------------------------------------------------------------
# Caches the expensive, SOURCE-dependent (slider-independent) parts of building
# a texture set's preview - loading/downscaling files, baking AO - so that moving
# a single slider only has to redo the cheap, slider-dependent composition step
# for the map(s) that slider actually affects, instead of rebuilding all four
# textures from scratch at full resolution every time.
# ------------------------------------------------------------------------------
class SetPreviewCache:
    def __init__(self, diff_path: Path, norm_path: Path, spec_path: Path):
        self.diff_path = diff_path
        self.norm_path = norm_path
        self.spec_path = spec_path
        self._loaded = False

        self.size = (PREVIEW_RES, PREVIEW_RES)
        self.has_alpha = False
        self.diffuse_base = None
        self.normal_raw = None
        self.normal_rgb = None
        self.spec_chan = None
        self._spec_rgba = None
        self.ao_white = None
        self.ao_baked = None
        self._ao_baked_ready = False
        self._avg_color = None

    def ensure_loaded(self):
        if self._loaded:
            return

        if self.diff_path and self.diff_path.exists():
            raw = Image.open(self.diff_path)
            self.has_alpha = raw.mode in ("RGBA", "LA") or (raw.mode == "P" and "transparency" in raw.info)
            self.diffuse_base = _downscale(raw.convert("RGBA"), PREVIEW_RES)
            self.size = self.diffuse_base.size
        else:
            self.diffuse_base = None

        if self.norm_path and self.norm_path.exists():
            n_raw = _downscale(Image.open(self.norm_path).convert("RGB"), PREVIEW_RES)
            if n_raw.size != self.size:
                n_raw = n_raw.resize(self.size, Image.Resampling.LANCZOS)
            self.normal_raw = n_raw
            self.normal_rgb = process_fo76_normal_map_from_image(n_raw)

        if self.spec_path and self.spec_path.exists():
            s_raw = _downscale(Image.open(self.spec_path).convert("RGBA"), PREVIEW_RES)
            if s_raw.size != self.size:
                s_raw = s_raw.resize(self.size, Image.Resampling.LANCZOS)
            self._spec_rgba = s_raw
            self.spec_chan = s_raw.getchannel("G")

        self.ao_white = Image.new("L", self.size, 255)
        self._loaded = True

    def get_ao(self, use_white_ao: bool, ao_strength: float = 1.4):
        self.ensure_loaded()
        if use_white_ao:
            return self.ao_white
        if not self._ao_baked_ready:
            # This is the expensive step (gradient + blur) - only ever computed once
            # per texture set, and only if "white AO" is turned off.
            self.ao_baked = bake_ao_from_normal_and_diffuse(
                None, self.diffuse_base, self.size,
                ao_strength=ao_strength, normal_img=self.normal_raw
            )
            self._ao_baked_ready = True
        return self.ao_baked

    def get_diffuse(self, light_mult: float):
        self.ensure_loaded()
        if self.diffuse_base is None:
            return None
        img = self.diffuse_base
        if light_mult != 1.0:
            r, g, b, a = img.split()
            rgb = Image.merge("RGB", (r, g, b))
            rgb = Image.eval(rgb, lambda x: int(min(255, x * light_mult)))
            r, g, b = rgb.split()
            img = Image.merge("RGBA", (r, g, b, a))
        if not self.has_alpha:
            r, g, b, _ = img.split()
            img = Image.merge("RGBA", (r, g, b, Image.new("L", self.size, 255)))
        return img

    def get_normal(self):
        self.ensure_loaded()
        if self.normal_rgb is None:
            return None
        return Image.merge("RGBA", (*self.normal_rgb.split(), Image.new("L", self.size, 255)))

    def get_reflection(self, metal_offset: int, reflection_boost: float, use_env_mask: bool = False,
                        use_spec_tint: bool = False, tint_rgb: tuple = None,
                        gate_bias_by_spec: bool = False):
        self.ensure_loaded()
        if self.diffuse_base is None:
            return None
        img_r = process_fo76_reflection_map(
            self.diffuse_base, self.spec_path, self.size,
            reflection_strength=reflection_boost, bias=metal_offset / 255.0,
            spec_img=self._spec_rgba, use_env_mask=use_env_mask, use_spec_tint=use_spec_tint,
            tint_rgb=tint_rgb, gate_bias_by_spec=gate_bias_by_spec
        )
        return Image.merge("RGBA", (*img_r.split(), Image.new("L", self.size, 255)))

    def get_lightmap(self, use_white_ao: bool, sss_strength: int, ao_strength: float = 1.4):
        self.ensure_loaded()
        ao_chan = self.get_ao(use_white_ao, ao_strength)
        spec_chan = self.spec_chan if self.spec_chan is not None else Image.new("L", self.size, 128)
        sss_chan = Image.new("L", self.size, int(min(255, max(0, sss_strength))))
        img_l = Image.merge("RGB", (spec_chan, ao_chan, sss_chan))
        return Image.merge("RGBA", (*img_l.split(), Image.new("L", self.size, 255)))

    def get_average_diffuse_color(self):
        # Average color of the diffuse texture, used as the base color for the
        # material-ball preview. Cached since it only needs computing once.
        self.ensure_loaded()
        if self.diffuse_base is None:
            return (180, 180, 185)
        if self._avg_color is None:
            stat = ImageStat.Stat(self.diffuse_base.convert("RGB"))
            self._avg_color = tuple(int(v) for v in stat.mean)
        return self._avg_color


# ------------------------------------------------------------------------------
# Per-texture edit state: brush painting (per RGBA channel, with soft mask
# blending) + global color/channel adjustments. One of these is kept per
# (texture-set, map-type) combination.
# ------------------------------------------------------------------------------
class TextureEditState:
    def __init__(self, size):
        self.size = size
        self.masks = {c: Image.new("L", size, 0) for c in "RGBA"}
        self.values = {c: Image.new("L", size, 0) for c in "RGBA"}

        # Global adjustments
        self.brightness = 1.0
        self.contrast = 1.0
        self.saturation = 1.0
        self.alpha_mult = 1.0
        self.channel_gain = {"R": 1.0, "G": 1.0, "B": 1.0, "A": 1.0}
        self.channel_offset = {"R": 0, "G": 0, "B": 0, "A": 0}

    def is_blank(self):
        no_paint = all(m.getextrema()[1] == 0 for m in self.masks.values())
        no_adjust = (self.brightness == 1.0 and self.contrast == 1.0 and
                     self.saturation == 1.0 and self.alpha_mult == 1.0 and
                     all(v == 1.0 for v in self.channel_gain.values()) and
                     all(v == 0 for v in self.channel_offset.values()))
        return no_paint and no_adjust

    def clone(self):
        clone = TextureEditState(self.size)
        clone.masks = {c: img.copy() for c, img in self.masks.items()}
        clone.values = {c: img.copy() for c, img in self.values.items()}
        clone.brightness, clone.contrast = self.brightness, self.contrast
        clone.saturation, clone.alpha_mult = self.saturation, self.alpha_mult
        clone.channel_gain = dict(self.channel_gain)
        clone.channel_offset = dict(self.channel_offset)
        return clone

    def restore_from(self, other: "TextureEditState"):
        self.masks = {c: img.copy() for c, img in other.masks.items()}
        self.values = {c: img.copy() for c, img in other.values.items()}
        self.brightness, self.contrast = other.brightness, other.contrast
        self.saturation, self.alpha_mult = other.saturation, other.alpha_mult
        self.channel_gain = dict(other.channel_gain)
        self.channel_offset = dict(other.channel_offset)

    def apply(self, base_img: "Image.Image") -> "Image.Image":

        img = base_img.convert("RGBA")

        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b))
        if self.brightness != 1.0:
            rgb = ImageEnhance.Brightness(rgb).enhance(self.brightness)
        if self.contrast != 1.0:
            rgb = ImageEnhance.Contrast(rgb).enhance(self.contrast)
        if self.saturation != 1.0:
            rgb = ImageEnhance.Color(rgb).enhance(self.saturation)
        r, g, b = rgb.split()

        if self.alpha_mult != 1.0:
            a = a.point(lambda v: int(min(255, max(0, v * self.alpha_mult))))

        chans = {"R": r, "G": g, "B": b, "A": a}
        adjusted = {}
        for c in "RGBA":
            gain = self.channel_gain[c]
            off = self.channel_offset[c]
            ch = chans[c]
            if gain != 1.0 or off != 0:
                ch = ch.point(lambda v, g=gain, o=off: int(min(255, max(0, v * g + o))))
            adjusted[c] = ch

        out = {}
        for c in "RGBA":
            mask = self.masks[c]
            if mask.getextrema()[1] == 0:
                out[c] = adjusted[c]
                continue
            values = self.values[c]
            if mask.size != img.size:
                mask = mask.resize(img.size, Image.Resampling.NEAREST)
                values = values.resize(img.size, Image.Resampling.NEAREST)
            base_arr = np.asarray(adjusted[c], dtype=np.float32)
            mask_arr = np.asarray(mask, dtype=np.float32) / 255.0
            val_arr = np.asarray(values, dtype=np.float32)
            result = base_arr * (1.0 - mask_arr) + val_arr * mask_arr
            out[c] = Image.fromarray(np.clip(result, 0, 255).astype(np.uint8), mode="L")

        return Image.merge("RGBA", (out["R"], out["G"], out["B"], out["A"]))


# ------------------------------------------------------------------------------
# Per-texture editor dialog: brush painting + adjustments for one map of one
# texture set.
# ------------------------------------------------------------------------------
class TextureEditorDialog(tk.Toplevel):
    MAX_DISPLAY = 640
    MAX_CANVAS_DIM = 4096 
    ZOOM_MIN, ZOOM_MAX = 0.1, 8.0
    SHAPE_TYPES = [("Rectangle", "rectangle"), ("Ellipse", "ellipse"),
                   ("Triangle", "triangle"), ("Line", "line")]

    def __init__(self, parent, title, base_image: "Image.Image",
                 edit_state: TextureEditState, map_kind: str,
                 default_color=(255, 255, 255),
                 palette_get=None, palette_add=None, palette_remove=None):
        super().__init__(parent)
        self.title(title)
        self.geometry("1080x740")
        self.minsize(760, 520)
        self.resizable(True, True)
        self.transient(parent)

        self.base_image = base_image.convert("RGBA")
        self.edit_state = edit_state
        self.map_kind = map_kind
        self.paint_color = default_color
        self.palette_get = palette_get
        self.palette_add = palette_add
        self.palette_remove = palette_remove

        w, h = self.base_image.size
        self.scale = min(1.0, self.MAX_DISPLAY / max(w, h))
        self.zoom = 1.0
        self.effective_scale = self.scale
        self.disp_size = (max(1, int(w * self.scale)), max(1, int(h * self.scale)))

        self._checker_cache = None
        self.img_item = None
        self.tk_img = None
        self.last_point = None
        self.undo_stack = []
        self._initial_snapshot = edit_state.clone()
        self._is_fullscreen = False

        # Brush cursor preview
        self.cursor_item = None
        self._last_hover = None

        # Shape tool state
        self.shape_start = None
        self.shape_preview_item = None
        self._shape_preview_type = None

        self.tool = tk.StringVar(value="erase_alpha" if map_kind == "diffuse" else "paint_channel")
        self.draw_mode = tk.StringVar(value="brush")
        self.shape_type = tk.StringVar(value="rectangle")
        self.shape_filled = tk.BooleanVar(value=True)

        self.brush_size = tk.IntVar(value=max(4, int(min(w, h) * 0.03)))
        self.paint_value = tk.IntVar(value=0)
        self.channels_enabled = {c: tk.BooleanVar(value=(c != "A")) for c in "RGBA"}

        self._build_ui()
        self._update_effective_scale()
        self._redraw()

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<F11>", lambda e: self._toggle_fullscreen())
        self.bind("<Escape>", self._on_escape)
        self.grab_set()

    # UI 
    def _build_ui(self):
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        # Canvas area (left, expands with window/fullscreen)
        canvas_area = ttk.Frame(outer)
        canvas_area.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        canvas_area.rowconfigure(1, weight=1)
        canvas_area.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(canvas_area)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))

        self.fullscreen_btn = ttk.Button(toolbar, text="Full Screen", command=self._toggle_fullscreen)
        self.fullscreen_btn.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(toolbar, text="Zoom:").pack(side=tk.LEFT)
        ttk.Button(toolbar, text="−", width=3, command=self._zoom_out).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(toolbar, text="+", width=3, command=self._zoom_in).pack(side=tk.LEFT, padx=(2, 8))
        ttk.Button(toolbar, text="Fit", command=self._zoom_fit).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="100%", command=self._zoom_actual).pack(side=tk.LEFT, padx=(2, 0))
        self.zoom_lbl = ttk.Label(toolbar, text="100%", width=6)
        self.zoom_lbl.pack(side=tk.LEFT, padx=(8, 0))

        self.canvas = tk.Canvas(canvas_area, bg="#202020", highlightthickness=1, highlightbackground="#444")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        vbar = ttk.Scrollbar(canvas_area, orient=tk.VERTICAL, command=self.canvas.yview)
        vbar.grid(row=1, column=1, sticky="ns")
        hbar = ttk.Scrollbar(canvas_area, orient=tk.HORIZONTAL, command=self.canvas.xview)
        hbar.grid(row=2, column=0, sticky="ew")
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Leave>", lambda e: self._clear_brush_cursor())

        # Ctrl+wheel to zoom; plain wheel/shift-wheel to pan (Windows/Mac + Linux)
        self.canvas.bind("<Control-MouseWheel>", self._on_wheel_zoom)
        self.canvas.bind("<Control-Button-4>", lambda e: self._zoom_in())
        self.canvas.bind("<Control-Button-5>", lambda e: self._zoom_out())
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        self.canvas.bind("<Shift-MouseWheel>", lambda e: self.canvas.xview_scroll(int(-1 * (e.delta / 120)), "units"))
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

        hint = ("Drag to paint (or drag out a shape). Ctrl+Scroll or the +/- buttons to zoom, "
                "scrollbars/wheel to pan, Full Screen for more room. Edits are always applied "
                "at full texture resolution on export, regardless of zoom.")
        ttk.Label(canvas_area, text=hint, wraplength=560, foreground="#888").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Controls (right, fixed width)
        controls = ttk.Frame(outer)
        controls.grid(row=0, column=1, sticky="ns")

        ttk.Label(controls, text=f"Editing: {MAP_LABELS.get(self.map_kind, self.map_kind)}",
                  font=("Helvetica", 10, "bold")).pack(anchor=tk.W, pady=(0, 6))

        # Brush size (shared by brush tool and shape outline thickness)
        brush_frame = ttk.Frame(controls)
        brush_frame.pack(fill=tk.X, pady=2)
        ttk.Label(brush_frame, text="Brush / Line Size:").pack(side=tk.LEFT)
        ttk.Scale(brush_frame, from_=2, to=400, variable=self.brush_size, orient=tk.HORIZONTAL,
                  length=120, command=lambda v: self._on_brush_size_change()).pack(side=tk.LEFT, padx=4)
        ttk.Label(brush_frame, textvariable=self.brush_size, width=4).pack(side=tk.LEFT)

        ttk.Separator(controls).pack(fill=tk.X, pady=6)

        self._build_draw_mode_section(controls)

        ttk.Separator(controls).pack(fill=tk.X, pady=6)

        if self.map_kind == "diffuse":
            self._build_diffuse_tools(controls)
        else:
            self._build_channel_tools(controls)

        ttk.Separator(controls).pack(fill=tk.X, pady=6)

        # Global adjustments
        if self.map_kind == "diffuse":
            self._build_diffuse_adjustments(controls)
        else:
            self._build_channel_adjustments(controls)

        ttk.Separator(controls).pack(fill=tk.X, pady=6)

        btn_row = ttk.Frame(controls)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btn_row, text="Undo", command=self._undo).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Reset All", command=self._reset).pack(side=tk.LEFT, padx=2)

        btn_row2 = ttk.Frame(controls)
        btn_row2.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btn_row2, text="Save & Close", command=self._on_save).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row2, text="Cancel", command=self._on_cancel).pack(side=tk.LEFT, padx=2)

    def _build_draw_mode_section(self, parent):
        ttk.Label(parent, text="Draw With:", font=("Helvetica", 9, "bold")).pack(anchor=tk.W)
        mode_row = ttk.Frame(parent)
        mode_row.pack(fill=tk.X, pady=2)
        ttk.Radiobutton(mode_row, text="Brush", variable=self.draw_mode, value="brush",
                         command=self._on_draw_mode_change).pack(side=tk.LEFT)
        ttk.Radiobutton(mode_row, text="Shape", variable=self.draw_mode, value="shape",
                         command=self._on_draw_mode_change).pack(side=tk.LEFT)

        self.shape_options_frame = ttk.Frame(parent)
        self.shape_options_frame.pack(fill=tk.X, pady=2)
        shape_row = ttk.Frame(self.shape_options_frame)
        shape_row.pack(fill=tk.X)
        for label, val in self.SHAPE_TYPES:
            ttk.Radiobutton(shape_row, text=label, variable=self.shape_type, value=val).pack(side=tk.LEFT)
        ttk.Checkbutton(self.shape_options_frame, text="Filled (uncheck for outline only)",
                         variable=self.shape_filled).pack(anchor=tk.W, pady=(2, 0))

        self._on_draw_mode_change()

    def _on_draw_mode_change(self):
        if self.draw_mode.get() == "shape":
            self.shape_options_frame.pack(fill=tk.X, pady=2)
            self._clear_brush_cursor()
        else:
            self.shape_options_frame.pack_forget()
            self._clear_shape_preview()
            self.shape_start = None

    def _build_diffuse_tools(self, parent):
        ttk.Label(parent, text="Paint Mode:", font=("Helvetica", 9, "bold")).pack(anchor=tk.W)
        ttk.Radiobutton(parent, text="Erase (make transparent)", variable=self.tool,
                         value="erase_alpha").pack(anchor=tk.W)
        ttk.Radiobutton(parent, text="Restore Alpha (make opaque)", variable=self.tool,
                         value="restore_alpha").pack(anchor=tk.W)
        ttk.Radiobutton(parent, text="Paint Color", variable=self.tool,
                         value="paint_color").pack(anchor=tk.W)

        color_row = ttk.Frame(parent)
        color_row.pack(fill=tk.X, pady=4)
        ttk.Label(color_row, text="Color:").pack(side=tk.LEFT)
        self.color_swatch = tk.Canvas(color_row, width=24, height=18, relief=tk.SUNKEN, bd=1,
                                       bg=self._hex(self.paint_color))
        self.color_swatch.pack(side=tk.LEFT, padx=4)
        ttk.Button(color_row, text="Pick...", command=self._pick_paint_color).pack(side=tk.LEFT)

        if self.palette_get is not None:
            ttk.Label(parent, text="Saved Colors (right-click to remove):").pack(anchor=tk.W, pady=(2, 0))
            self.palette_frame = ttk.Frame(parent)
            self.palette_frame.pack(fill=tk.X, pady=2)
            self._refresh_palette_swatches()

    def _refresh_palette_swatches(self):
        if self.palette_get is None:
            return
        for w in self.palette_frame.winfo_children():
            w.destroy()
        colors = self.palette_get()
        if not colors:
            ttk.Label(self.palette_frame, text="(none yet)", foreground="#666").pack(side=tk.LEFT)
            return
        for rgb in colors:
            b = tk.Button(self.palette_frame, bg=self._hex(rgb), width=2, height=1,
                          relief=tk.RAISED, command=lambda c=rgb: self._select_palette_color(c))
            if self.palette_remove is not None:
                b.bind("<Button-3>", lambda e, c=rgb: self._remove_palette_color(c))
            b.pack(side=tk.LEFT, padx=1, pady=1)

    def _remove_palette_color(self, rgb):
        if self.palette_remove is not None:
            self.palette_remove(rgb)
            self._refresh_palette_swatches()

    def _select_palette_color(self, rgb):
        self.paint_color = rgb
        self.color_swatch.config(bg=self._hex(rgb))

    def _build_diffuse_adjustments(self, parent):
        ttk.Label(parent, text="Adjustments:", font=("Helvetica", 9, "bold")).pack(anchor=tk.W)
        self.brightness_var = tk.DoubleVar(value=self.edit_state.brightness)
        self.contrast_var = tk.DoubleVar(value=self.edit_state.contrast)
        self.saturation_var = tk.DoubleVar(value=self.edit_state.saturation)
        self.alpha_var = tk.DoubleVar(value=self.edit_state.alpha_mult)

        self._slider_row(parent, "Brightness", self.brightness_var, 0.2, 2.0, self._on_brightness)
        self._slider_row(parent, "Contrast", self.contrast_var, 0.2, 2.0, self._on_contrast)
        self._slider_row(parent, "Saturation", self.saturation_var, 0.0, 2.0, self._on_saturation)
        self._slider_row(parent, "Alpha Opacity", self.alpha_var, 0.0, 1.0, self._on_alpha_mult)

    def _build_channel_tools(self, parent):
        ttk.Label(parent, text="Paint Channels:", font=("Helvetica", 9, "bold")).pack(anchor=tk.W)
        chan_row = ttk.Frame(parent)
        chan_row.pack(fill=tk.X, pady=2)
        labels = {"R": "R", "G": "G", "B": "B", "A": "A (unused)"}
        for c in "RGBA":
            ttk.Checkbutton(chan_row, text=labels[c], variable=self.channels_enabled[c],
                             command=self._on_channel_filter_change).pack(side=tk.LEFT)
        ttk.Label(parent, text="Preview shows only the checked channel(s) above.",
                  foreground="#888").pack(anchor=tk.W, pady=(0, 2))

        val_row = ttk.Frame(parent)
        val_row.pack(fill=tk.X, pady=4)
        ttk.Label(val_row, text="Paint Value:").pack(side=tk.LEFT)
        ttk.Scale(val_row, from_=0, to=255, variable=self.paint_value,
                  orient=tk.HORIZONTAL, length=140).pack(side=tk.LEFT, padx=4)
        val_lbl = ttk.Label(val_row, textvariable=self.paint_value, width=4)
        val_lbl.pack(side=tk.LEFT)

        quick_row = ttk.Frame(parent)
        quick_row.pack(fill=tk.X, pady=2)
        ttk.Button(quick_row, text="Set 0 (Erase)", command=lambda: self.paint_value.set(0)).pack(side=tk.LEFT, padx=2)
        ttk.Button(quick_row, text="Set 128", command=lambda: self.paint_value.set(128)).pack(side=tk.LEFT, padx=2)
        ttk.Button(quick_row, text="Set 255 (Fill)", command=lambda: self.paint_value.set(255)).pack(side=tk.LEFT, padx=2)


    def _build_channel_adjustments(self, parent):
        ttk.Label(parent, text="Per-Channel Gain / Offset:", font=("Helvetica", 9, "bold")).pack(anchor=tk.W)
        self.gain_vars = {}
        self.offset_vars = {}
        for c in "RGB":
            gvar = tk.DoubleVar(value=self.edit_state.channel_gain[c])
            ovar = tk.DoubleVar(value=self.edit_state.channel_offset[c])
            self.gain_vars[c] = gvar
            self.offset_vars[c] = ovar
            row = ttk.Frame(parent)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=f"{c} Gain:", width=8).pack(side=tk.LEFT)
            ttk.Scale(row, from_=0.0, to=2.0, variable=gvar, orient=tk.HORIZONTAL, length=90,
                      command=lambda v, ch=c: self._on_channel_gain(ch)).pack(side=tk.LEFT)
            ttk.Label(row, text="Offset:").pack(side=tk.LEFT, padx=(6, 0))
            ttk.Scale(row, from_=-128, to=128, variable=ovar, orient=tk.HORIZONTAL, length=90,
                      command=lambda v, ch=c: self._on_channel_offset(ch)).pack(side=tk.LEFT)

    def _slider_row(self, parent, label, var, lo, hi, callback):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=1)
        ttk.Label(row, text=label, width=12).pack(side=tk.LEFT)
        ttk.Scale(row, from_=lo, to=hi, variable=var, orient=tk.HORIZONTAL, length=140,
                  command=lambda v: callback()).pack(side=tk.LEFT, padx=4)

    @staticmethod
    def _hex(rgb):
        return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

    def _pick_paint_color(self):
        result = colorchooser.askcolor(title="Pick Paint Color", initialcolor=self._hex(self.paint_color))
        if result and result[0]:
            self.paint_color = tuple(int(c) for c in result[0])
            self.color_swatch.config(bg=self._hex(self.paint_color))
            if self.palette_add is not None:
                self.palette_add(self.paint_color)
                self._refresh_palette_swatches()

    # Full screen / zoom
    def _toggle_fullscreen(self):
        self._is_fullscreen = not self._is_fullscreen
        try:
            self.attributes("-fullscreen", self._is_fullscreen)
        except tk.TclError:
            self.state("zoomed" if self._is_fullscreen else "normal")
        self.fullscreen_btn.config(text="Exit Full Screen" if self._is_fullscreen else "Full Screen")

    def _on_escape(self, event=None):
        if self._is_fullscreen:
            self._toggle_fullscreen()

    def _on_wheel_zoom(self, event):
        if event.delta > 0:
            self._zoom_in()
        else:
            self._zoom_out()

    def _zoom_in(self):
        self._set_zoom(self.zoom * 1.25)

    def _zoom_out(self):
        self._set_zoom(self.zoom / 1.25)

    def _zoom_fit(self):
        self._set_zoom(1.0)

    def _zoom_actual(self):
        self._set_zoom(1.0 / self.scale if self.scale > 0 else 1.0)

    def _set_zoom(self, new_zoom):
        new_zoom = max(self.ZOOM_MIN, min(self.ZOOM_MAX, new_zoom))
        if abs(new_zoom - self.zoom) < 1e-6:
            return
        self.zoom = new_zoom
        self._update_effective_scale()
        self._redraw()
        if self._last_hover and self.draw_mode.get() == "brush":
            self._update_brush_cursor(*self._last_hover)

    def _update_effective_scale(self):
        self.effective_scale = self.scale * self.zoom
        w, h = self.base_image.size
        disp_w = max(1, int(w * self.effective_scale))
        disp_h = max(1, int(h * self.effective_scale))
        if max(disp_w, disp_h) > self.MAX_CANVAS_DIM:
            shrink = self.MAX_CANVAS_DIM / max(disp_w, disp_h)
            self.effective_scale *= shrink
            disp_w = max(1, int(w * self.effective_scale))
            disp_h = max(1, int(h * self.effective_scale))
        self.disp_size = (disp_w, disp_h)
        self.canvas.configure(scrollregion=(0, 0, disp_w, disp_h))
        self.zoom_lbl.config(text=f"{self.effective_scale * 100:.0f}%")

    # Painting
    def _to_image_coords(self, cx, cy):
        return cx / self.effective_scale, cy / self.effective_scale

    def _stamp_channel(self, chan, value, bbox):
        ImageDraw.Draw(self.edit_state.masks[chan]).ellipse(bbox, fill=255)
        ImageDraw.Draw(self.edit_state.values[chan]).ellipse(bbox, fill=int(max(0, min(255, value))))

    def _paint_at(self, cx, cy):
        ix, iy = self._to_image_coords(cx, cy)
        rad = max(1.0, self.brush_size.get() / 2.0)
        bbox = [ix - rad, iy - rad, ix + rad, iy + rad]
        tool = self.tool.get()

        if tool == "erase_alpha":
            self._stamp_channel("A", 0, bbox)
        elif tool == "restore_alpha":
            self._stamp_channel("A", 255, bbox)
        elif tool == "paint_color":
            r, g, b = self.paint_color
            self._stamp_channel("R", r, bbox)
            self._stamp_channel("G", g, bbox)
            self._stamp_channel("B", b, bbox)
        elif tool == "paint_channel":
            val = self.paint_value.get()
            for c in "RGBA":
                if self.channels_enabled[c].get():
                    self._stamp_channel(c, val, bbox)

    def _paint_line(self, p0, p1):
        x0, y0 = p0
        x1, y1 = p1
        dist = math.hypot(x1 - x0, y1 - y0)
        step = max(1.0, (self.brush_size.get() * self.effective_scale) / 3.0)
        steps = max(1, int(dist / step))
        for i in range(steps + 1):
            t = i / steps
            self._paint_at(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)

    # Shape tool
    def _stamp_shape_channel(self, chan, value, x0, y0, x1, y1, shape_type, filled, width):
        mask_draw = ImageDraw.Draw(self.edit_state.masks[chan])
        val_draw = ImageDraw.Draw(self.edit_state.values[chan])
        v = int(max(0, min(255, value)))

        if shape_type == "rectangle":
            if filled:
                mask_draw.rectangle([x0, y0, x1, y1], fill=255)
                val_draw.rectangle([x0, y0, x1, y1], fill=v)
            else:
                mask_draw.rectangle([x0, y0, x1, y1], outline=255, width=width)
                val_draw.rectangle([x0, y0, x1, y1], outline=v, width=width)
        elif shape_type == "ellipse":
            if filled:
                mask_draw.ellipse([x0, y0, x1, y1], fill=255)
                val_draw.ellipse([x0, y0, x1, y1], fill=v)
            else:
                mask_draw.ellipse([x0, y0, x1, y1], outline=255, width=width)
                val_draw.ellipse([x0, y0, x1, y1], outline=v, width=width)
        elif shape_type == "triangle":
            cx = (x0 + x1) / 2.0
            pts = [(cx, y0), (x0, y1), (x1, y1)]
            if filled:
                mask_draw.polygon(pts, fill=255)
                val_draw.polygon(pts, fill=v)
            else:
                closed = pts + [pts[0]]
                mask_draw.line(closed, fill=255, width=width, joint="curve")
                val_draw.line(closed, fill=v, width=width, joint="curve")
        elif shape_type == "line":
            mask_draw.line([(x0, y0), (x1, y1)], fill=255, width=width)
            val_draw.line([(x0, y0), (x1, y1)], fill=v, width=width)

    def _commit_shape(self, cx0, cy0, cx1, cy1):
        ix0, iy0 = self._to_image_coords(cx0, cy0)
        ix1, iy1 = self._to_image_coords(cx1, cy1)
        shape_type = self.shape_type.get()
        filled = self.shape_filled.get()
        width = max(1, int(round(self.brush_size.get())))

        if shape_type in ("rectangle", "ellipse", "triangle"):
            x0, x1 = sorted((ix0, ix1))
            y0, y1 = sorted((iy0, iy1))
            if x1 - x0 < 1 or y1 - y0 < 1:
                return
        else:
            x0, y0, x1, y1 = ix0, iy0, ix1, iy1
            if abs(x1 - x0) < 1 and abs(y1 - y0) < 1:
                return

        tool = self.tool.get()
        if tool == "erase_alpha":
            self._stamp_shape_channel("A", 0, x0, y0, x1, y1, shape_type, filled, width)
        elif tool == "restore_alpha":
            self._stamp_shape_channel("A", 255, x0, y0, x1, y1, shape_type, filled, width)
        elif tool == "paint_color":
            r, g, b = self.paint_color
            self._stamp_shape_channel("R", r, x0, y0, x1, y1, shape_type, filled, width)
            self._stamp_shape_channel("G", g, x0, y0, x1, y1, shape_type, filled, width)
            self._stamp_shape_channel("B", b, x0, y0, x1, y1, shape_type, filled, width)
        elif tool == "paint_channel":
            val = self.paint_value.get()
            for c in "RGBA":
                if self.channels_enabled[c].get():
                    self._stamp_shape_channel(c, val, x0, y0, x1, y1, shape_type, filled, width)

    def _create_shape_preview(self, x0, y0, x1, y1):
        self._clear_shape_preview()
        st = self.shape_type.get()
        color = "#39ff88"
        if st == "rectangle":
            self.shape_preview_item = self.canvas.create_rectangle(x0, y0, x1, y1, outline=color,
                                                                     width=2, dash=(4, 2))
        elif st == "ellipse":
            self.shape_preview_item = self.canvas.create_oval(x0, y0, x1, y1, outline=color, width=2)
        elif st == "triangle":
            cx = (x0 + x1) / 2
            pts = [cx, y0, x0, y1, x1, y1]
            self.shape_preview_item = self.canvas.create_polygon(pts, outline=color, width=2,
                                                                   fill="", dash=(4, 2))
        elif st == "line":
            self.shape_preview_item = self.canvas.create_line(x0, y0, x1, y1, fill=color,
                                                                width=2, dash=(4, 2))
        self._shape_preview_type = st

    def _update_shape_preview(self, x0, y0, x1, y1):
        st = self.shape_type.get()
        if self.shape_preview_item is None or self._shape_preview_type != st:
            self._create_shape_preview(x0, y0, x1, y1)
            return
        if st in ("rectangle", "ellipse", "line"):
            self.canvas.coords(self.shape_preview_item, x0, y0, x1, y1)
        elif st == "triangle":
            cx = (x0 + x1) / 2
            self.canvas.coords(self.shape_preview_item, cx, y0, x0, y1, x1, y1)

    def _clear_shape_preview(self):
        if self.shape_preview_item is not None:
            self.canvas.delete(self.shape_preview_item)
            self.shape_preview_item = None
            self._shape_preview_type = None

    # Brush cursor preview
    def _on_hover(self, event):
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        self._last_hover = (cx, cy)
        if self.draw_mode.get() == "brush":
            self._update_brush_cursor(cx, cy)
        else:
            self._clear_brush_cursor()

    def _on_brush_size_change(self):
        if self._last_hover and self.draw_mode.get() == "brush":
            self._update_brush_cursor(*self._last_hover)

    def _update_brush_cursor(self, cx, cy):
        rad_screen = max(2.0, (self.brush_size.get() / 2.0) * self.effective_scale)
        bbox = [cx - rad_screen, cy - rad_screen, cx + rad_screen, cy + rad_screen]

        if self.cursor_item is None:
            self.cursor_item = self.canvas.create_oval(*bbox, outline="#39ff88", width=2)
        else:
            self.canvas.coords(self.cursor_item, *bbox)
        self.canvas.tag_raise(self.cursor_item)


    def _clear_brush_cursor(self):
        if self.cursor_item is not None:
            self.canvas.delete(self.cursor_item)
            self.cursor_item = None

    # Mouse events
    def _on_press(self, event):
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        self._push_undo()
        if self.draw_mode.get() == "shape":
            self.shape_start = (cx, cy)
            self._create_shape_preview(cx, cy, cx, cy)
        else:
            self.last_point = (cx, cy)
            self._paint_at(cx, cy)
            self._redraw()

    def _on_drag(self, event):
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if self.draw_mode.get() == "shape":
            if self.shape_start is not None:
                self._update_shape_preview(self.shape_start[0], self.shape_start[1], cx, cy)
        else:
            if self.last_point:
                self._paint_line(self.last_point, (cx, cy))
            else:
                self._paint_at(cx, cy)
            self.last_point = (cx, cy)
            self._redraw()

    def _on_release(self, event):
        cx, cy = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        if self.draw_mode.get() == "shape" and self.shape_start is not None:
            self._commit_shape(self.shape_start[0], self.shape_start[1], cx, cy)
            self._clear_shape_preview()
            self.shape_start = None
            self._redraw()
        self.last_point = None

    # Slider callbacks
    def _on_brightness(self):
        self.edit_state.brightness = float(self.brightness_var.get())
        self._redraw()

    def _on_contrast(self):
        self.edit_state.contrast = float(self.contrast_var.get())
        self._redraw()

    def _on_saturation(self):
        self.edit_state.saturation = float(self.saturation_var.get())
        self._redraw()

    def _on_alpha_mult(self):
        self.edit_state.alpha_mult = float(self.alpha_var.get())
        self._redraw()

    def _on_channel_gain(self, chan):
        self.edit_state.channel_gain[chan] = float(self.gain_vars[chan].get())
        self._redraw()

    def _on_channel_offset(self, chan):
        self.edit_state.channel_offset[chan] = float(self.offset_vars[chan].get())
        self._redraw()

    # Undo / Reset
    def _push_undo(self):
        self.undo_stack.append(self.edit_state.clone())
        if len(self.undo_stack) > 20:
            self.undo_stack.pop(0)

    def _undo(self):
        if not self.undo_stack:
            return
        snap = self.undo_stack.pop()
        self.edit_state.restore_from(snap)
        self._sync_controls()
        self._redraw()

    def _reset(self):
        self._push_undo()
        blank = TextureEditState(self.base_image.size)
        self.edit_state.restore_from(blank)
        self._sync_controls()
        self._redraw()

    def _sync_controls(self):
        if self.map_kind == "diffuse":
            self.brightness_var.set(self.edit_state.brightness)
            self.contrast_var.set(self.edit_state.contrast)
            self.saturation_var.set(self.edit_state.saturation)
            self.alpha_var.set(self.edit_state.alpha_mult)
        else:
            for c in "RGB":
                self.gain_vars[c].set(self.edit_state.channel_gain[c])
                self.offset_vars[c].set(self.edit_state.channel_offset[c])

    # Render
    def _checkerboard(self, size):
        if self._checker_cache and self._checker_cache[0] == size:
            return self._checker_cache[1]
        cb = Image.new("RGB", size, (190, 190, 190))
        draw = ImageDraw.Draw(cb)
        step = 12
        for y in range(0, size[1], step):
            for x in range(0, size[0], step):
                if (x // step + y // step) % 2 == 0:
                    draw.rectangle([x, y, x + step, y + step], fill=(140, 140, 140))
        self._checker_cache = (size, cb)
        return cb

    def _on_channel_filter_change(self):
        self._redraw()

    def _filter_to_selected_channels(self, img: "Image.Image") -> "Image.Image":
        enabled = {c: self.channels_enabled[c].get() for c in "RGB"}
        if all(enabled.values()) or not any(enabled.values()):
            # Nothing to filter (all on), or nothing selected (avoid showing pure
            # black, which would just be confusing) - show the image as-is.
            return img
        r, g, b, a = img.split()
        zero = Image.new("L", img.size, 0)
        r = r if enabled["R"] else zero
        g = g if enabled["G"] else zero
        b = b if enabled["B"] else zero
        return Image.merge("RGBA", (r, g, b, a))

    def _redraw(self):
        composed = self.edit_state.apply(self.base_image)

        if self.map_kind != "diffuse":
            composed = self._filter_to_selected_channels(composed)

        disp_rgba = composed.resize(self.disp_size, Image.Resampling.NEAREST)

        if self.map_kind == "diffuse":
            bg = self._checkerboard(self.disp_size).copy()
            bg.paste(disp_rgba.convert("RGB"), (0, 0), disp_rgba.getchannel("A"))
            disp = bg
        else:
            disp = disp_rgba.convert("RGB")

        self.tk_img = ImageTk.PhotoImage(disp)
        if self.img_item is None:
            self.img_item = self.canvas.create_image(0, 0, image=self.tk_img, anchor=tk.NW)
        else:
            self.canvas.itemconfig(self.img_item, image=self.tk_img)
        self.canvas.tag_lower(self.img_item)

    # Close
    def _on_save(self):
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.edit_state.restore_from(self._initial_snapshot)
        self.grab_release()
        self.destroy()


# ------------------------------------------------------------------------------
# PBR Image Conversion Pipeline
# ------------------------------------------------------------------------------
# A single "encode this image to this DDS file" unit of work. Preparing these
# (build_fo76_images + edits + format selection) is fast; save_dds/texconv is
# what actually takes time, and each job's texconv call is fully independent
# of every other job - so a whole batch's jobs can be encoded in parallel.
SaveJob = namedtuple("SaveJob", ["image", "out_path", "dxgi_format", "is_normal_map", "label"])


def prepare_texture_set_save_jobs(diffuse_path: Path, normal_path: Path, spec_path: Path,
                                  output_dir: Path,
                                  metal_offset: int, light_mult: float,
                                  reflection_boost: float = 1.6,
                                  use_white_ao: bool = True,
                                  ao_strength: float = 1.4,
                                  sss_strength: int = 0,
                                  use_env_mask: bool = False,
                                  use_spec_tint: bool = False,
                                  tint_rgb: tuple = None,
                                  gate_bias_by_spec: bool = False,
                                  use_bc7_diffuse: bool = False,
                                  use_bc7_reflection: bool = False,
                                  texture_edits: dict = None,
                                  enabled_maps: set = None,
                                  output_base_name: str = None) -> list:
    
    base_name = output_base_name or diffuse_path.stem.rsplit('_', 1)[0]
    output_dir.mkdir(parents=True, exist_ok=True)
    texture_edits = texture_edits or {}
    if enabled_maps is None:
        enabled_maps = {"diffuse", "normal", "reflection", "lightmap"}
    if not enabled_maps:
        return []

    images, has_alpha, target_size = build_fo76_images(
        diffuse_path, normal_path, spec_path,
        metal_offset, light_mult,
        reflection_boost=reflection_boost, use_white_ao=use_white_ao,
        ao_strength=ao_strength, sss_strength=sss_strength,
        use_env_mask=use_env_mask, use_spec_tint=use_spec_tint,
        tint_rgb=tint_rgb, gate_bias_by_spec=gate_bias_by_spec,
        enabled_maps=enabled_maps
    )

    for kind in list(images.keys()):
        edit_state = texture_edits.get(kind)
        if edit_state is not None:
            images[kind] = edit_state.apply(images[kind])

    jobs = []

    if "diffuse" in images:
        img_d = images["diffuse"]
        alpha_min, _ = img_d.getchannel("A").getextrema()
        final_has_alpha = has_alpha or alpha_min < 255
        img_d_out = img_d if final_has_alpha else img_d.convert("RGB")
        if use_bc7_diffuse:
            bc_fmt = "BC7_UNORM_SRGB"
        else:
            bc_fmt = "BC3_UNORM_SRGB" if final_has_alpha else "BC1_UNORM_SRGB"
        label = f"_d [{'BC7 sRGB' if use_bc7_diffuse else 'BC1/BC3 sRGB'}]"
        jobs.append(SaveJob(img_d_out, output_dir / f"{base_name}_d.dds", bc_fmt, False, label))

    if "normal" in images:
        jobs.append(SaveJob(images["normal"].convert("RGB"), output_dir / f"{base_name}_n.dds",
                             "BC5_SNORM", True, "_n [BC5_SNORM]"))

    if "reflection" in images:
        r_fmt = "BC7_UNORM_SRGB" if use_bc7_reflection else "BC1_UNORM_SRGB"
        label_bits = [('BC7 sRGB' if use_bc7_reflection else 'BC1 sRGB')]
        if use_env_mask:
            label_bits.append("envmask")
        if use_spec_tint or tint_rgb is not None:
            label_bits.append("tinted")
        if gate_bias_by_spec:
            label_bits.append("from .bgsm")
        label = f"_r [{', '.join(label_bits)}]"
        jobs.append(SaveJob(images["reflection"].convert("RGB"), output_dir / f"{base_name}_r.dds",
                             r_fmt, False, label))

    if "lightmap" in images:
        jobs.append(SaveJob(images["lightmap"].convert("RGB"), output_dir / f"{base_name}_l.dds",
                             "BC1_UNORM", False, "_l [BC1, Spec.G/AO(normal+diffuse)/SSS]"))

    return jobs


def process_texture_set_pbr(diffuse_path: Path, normal_path: Path, spec_path: Path,
                            output_dir: Path,
                            metal_offset: int, light_mult: float,
                            reflection_boost: float = 1.6,
                            use_white_ao: bool = True,
                            ao_strength: float = 1.4,
                            sss_strength: int = 0,
                            use_env_mask: bool = False,
                            use_spec_tint: bool = False,
                            tint_rgb: tuple = None,
                            gate_bias_by_spec: bool = False,
                            use_bc7_diffuse: bool = False,
                            use_bc7_reflection: bool = False,
                            texture_edits: dict = None,
                            enabled_maps: set = None,
                            log_func=print,
                            output_base_name: str = None):

    base_name = output_base_name or diffuse_path.stem.rsplit('_', 1)[0]
    jobs = prepare_texture_set_save_jobs(
        diffuse_path, normal_path, spec_path, output_dir,
        metal_offset, light_mult,
        reflection_boost=reflection_boost, use_white_ao=use_white_ao,
        ao_strength=ao_strength, sss_strength=sss_strength,
        use_env_mask=use_env_mask, use_spec_tint=use_spec_tint,
        tint_rgb=tint_rgb, gate_bias_by_spec=gate_bias_by_spec,
        use_bc7_diffuse=use_bc7_diffuse, use_bc7_reflection=use_bc7_reflection,
        texture_edits=texture_edits, enabled_maps=enabled_maps,
        output_base_name=output_base_name
    )

    if not jobs:
        log_func(f"  - Skipped {base_name}: no output types selected")
        return

    for job in jobs:
        save_dds(job.image, job.out_path, job.dxgi_format, is_normal_map=job.is_normal_map, log_func=log_func)

    log_func(f"  ✓ Saved FO76 set: {base_name} ({', '.join(j.label for j in jobs)})")


def get_set_key(diffuse_path: Path, in_dir: Path) -> str:

    rel = str(diffuse_path.relative_to(in_dir))
    if rel.endswith("_d.dds"):
        rel = rel[:-6]
    return rel


def find_set_files(diff_file: Path):
    base_prefix = str(diff_file)[:-6]
    norm_file = Path(f"{base_prefix}_n.dds")
    spec_file = Path(f"{base_prefix}_s.dds")
    if not spec_file.exists():
        spec_file = Path(f"{base_prefix}_r.dds")
    return norm_file, spec_file


# ------------------------------------------------------------------------------
# Main Application GUI
# ------------------------------------------------------------------------------
class FO76PBRStudioGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("FO4 → FO76 PBR Converter & Real-Time Material Studio")
        self.root.geometry("1300x950")
        self.root.minsize(820, 560)
        self.root.resizable(True, True)

        self.last_checked_input_path = ""

        # Persistent settings (custom colors, output toggles, etc) - survives restarts.
        self.settings = load_app_settings()
        self.default_brush_color = tuple(self.settings.get("default_brush_color", (180, 180, 185)))
        self.custom_colors = [tuple(c) for c in self.settings.get("custom_colors", [])]
        # name -> {"metal": int, "light": float, "spec": float, "white_ao": bool, "sss": int}
        self.custom_presets = dict(self.settings.get("custom_presets", {}))

        # set_key -> (diffuse_path, normal_path, spec_path)
        self.available_sets = {}
        # Parsed .bgsm materials (see build_bgsm_index) used by "pick material from .bgsm".
        self.bgsm_index = {}
        self.bgsm_materials = []          # [(BgsmInfo, path)] for every loaded .bgsm
        self.set_materials = {}           # set_key -> (BgsmInfo, path), when sets come from the .bgsm files
        self._materials_root = None
        self._last_materials_dir = None
        # set_key -> {'diffuse': TextureEditState, 'normal': ..., ...}
        self.texture_edits = {}
        self.current_set_key = None
        self.preview_photo_refs = {}
        self.preview_canvases = {}

        # Caches the expensive per-set work (SetPreviewCache) so slider moves only
        # redo cheap composition. Reset whenever the selected texture set changes.
        self.preview_cache = None
        # Coalesces rapid slider drag events into a single, targeted preview update.
        self._dirty_maps = set()
        self._debounce_job = None

        # Material-ball preview (alternate view to the flat per-texture tiles)
        self.sphere_renderer = PBRSphereRenderer(width=220, height=220)
        self.ball_photo = None

        self.create_widgets()
        if self.settings.get("materials_dir") and (self.settings.get("auto_material", False)
                                                   or self.settings.get("discover_from_bgsm", False)):
            self._rebuild_bgsm_index()

    def create_widgets(self):
        header = ttk.Frame(self.root, padding="10")
        header.pack(fill=tk.X)
        ttk.Label(header, text="FO76 Material & PBR Calibration Studio", font=("Helvetica", 14, "bold")).pack(anchor=tk.W)
        ttk.Label(header, text="Adjust live metalness, gloss, specular, and base color response, "
                               "then fine-tune each exported texture by hand before batch processing.").pack(anchor=tk.W)

        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        left_paned = ttk.PanedWindow(main_paned, orient=tk.VERTICAL)
        main_paned.add(left_paned, weight=3)

        controls_outer = ttk.Frame(left_paned)
        left_paned.add(controls_outer, weight=3)

        controls_canvas = tk.Canvas(controls_outer, highlightthickness=0)
        controls_scrollbar = ttk.Scrollbar(controls_outer, orient=tk.VERTICAL, command=controls_canvas.yview)
        controls_canvas.configure(yscrollcommand=controls_scrollbar.set)
        controls_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        controls_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        controls_frame = ttk.Frame(controls_canvas)
        controls_window_id = controls_canvas.create_window((0, 0), window=controls_frame, anchor="nw")

        def _sync_scrollregion(event=None):
            controls_canvas.configure(scrollregion=controls_canvas.bbox("all"))
        controls_frame.bind("<Configure>", _sync_scrollregion)

        def _sync_canvas_width(event):
            controls_canvas.itemconfig(controls_window_id, width=event.width)
        controls_canvas.bind("<Configure>", _sync_canvas_width)

        def _on_controls_mousewheel(event):
            controls_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        controls_canvas.bind("<Enter>", lambda e: controls_canvas.bind_all("<MouseWheel>", _on_controls_mousewheel))
        controls_canvas.bind("<Leave>", lambda e: controls_canvas.unbind_all("<MouseWheel>"))

        right_panel = ttk.LabelFrame(main_paned, text=" Texture Set Preview & Editing ", padding="10")
        main_paned.add(right_panel, weight=2)

        # 1. PATH SELECTION
        io_frame = ttk.LabelFrame(controls_frame, text=" Paths ", padding="8")
        io_frame.pack(fill=tk.X, pady=4)

        ttk.Label(io_frame, text="Input Dir:").grid(row=0, column=0, sticky=tk.W)
        self.input_entry = ttk.Entry(io_frame)
        self.input_entry.grid(row=0, column=1, sticky=tk.EW, padx=5, pady=2)
        ttk.Button(io_frame, text="Browse...", command=self.browse_input).grid(row=0, column=2, padx=2)

        self.input_entry.bind("<KeyRelease>", lambda e: self.check_and_update_input_path())
        self.input_entry.bind("<FocusOut>", lambda e: self.check_and_update_input_path())

        ttk.Label(io_frame, text="Output Dir:").grid(row=1, column=0, sticky=tk.W)
        self.output_entry = ttk.Entry(io_frame)
        self.output_entry.grid(row=1, column=1, sticky=tk.EW, padx=5, pady=2)
        ttk.Button(io_frame, text="Browse...", command=self.browse_output).grid(row=1, column=2, padx=2)

        ttk.Label(io_frame, text="Materials Dir:").grid(row=2, column=0, sticky=tk.W)
        self.materials_entry = ttk.Entry(io_frame)
        self.materials_entry.grid(row=2, column=1, sticky=tk.EW, padx=5, pady=2)
        self.materials_entry.insert(0, self.settings.get("materials_dir", ""))
        ttk.Button(io_frame, text="Browse...", command=self.browse_materials).grid(row=2, column=2, padx=2)
        self.materials_entry.bind("<FocusOut>", lambda e: self._on_materials_entry_changed())
        self.materials_entry.bind("<Return>", lambda e: self._on_materials_entry_changed())

        self.discover_var = tk.BooleanVar(value=self.settings.get("discover_from_bgsm", False))
        ttk.Checkbutton(
            io_frame,
            text="Find texture sets from the .bgsm files (use the texture names they list, "
                 "not _d/_n/_s names; output named after each .bgsm)",
            variable=self.discover_var, command=self.on_discover_toggle
        ).grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))

        self.auto_material_var = tk.BooleanVar(value=self.settings.get("auto_material", False))
        ttk.Checkbutton(
            io_frame,
            text="Pick material from the .bgsm's environment cubemap (overrides the preset/sliders below)",
            variable=self.auto_material_var, command=self.on_auto_material_toggle
        ).grid(row=4, column=0, columnspan=3, sticky=tk.W, pady=(0, 0))
        self.material_info_lbl = ttk.Label(io_frame, text="", foreground="#888", wraplength=420, justify=tk.LEFT)
        self.material_info_lbl.grid(row=5, column=0, columnspan=3, sticky=tk.W)

        ttk.Label(io_frame, text="FO76 Materials Out:").grid(row=6, column=0, sticky=tk.W, pady=(6, 0))
        self.fo76_mat_entry = ttk.Entry(io_frame)
        self.fo76_mat_entry.grid(row=6, column=1, sticky=tk.EW, padx=5, pady=(6, 2))
        self.fo76_mat_entry.insert(0, self.settings.get("fo76_materials_dir", ""))
        ttk.Button(io_frame, text="Browse...", command=self.browse_fo76_materials).grid(row=6, column=2, padx=2, pady=(6, 0))
        self.fo76_mat_entry.bind("<FocusOut>", lambda e: self._save_bgsm_output_settings())

        self.create_bgsm_var = tk.BooleanVar(value=self.settings.get("create_fo76_bgsm", False))
        ttk.Checkbutton(
            io_frame,
            text="Create an FO76 .bgsm for each converted set (points at the new _d/_n/_r/_l textures)",
            variable=self.create_bgsm_var, command=self._save_bgsm_output_settings
        ).grid(row=7, column=0, columnspan=3, sticky=tk.W, pady=(2, 0))
        ttk.Label(io_frame, text="Texture paths in the .bgsm are taken from after the last \"Textures\" folder "
                                 "in the output path, or else relative to Output Dir. Surface flags (two-sided, "
                                 "alpha test, etc.) are copied from the matching FO4 .bgsm when Materials Dir "
                                 "is set.",
                  foreground="#888", wraplength=420, justify=tk.LEFT
                  ).grid(row=8, column=0, columnspan=3, sticky=tk.W)

        io_frame.columnconfigure(1, weight=1)

        # 2. PBR SLIDERS & PRESETS
        slider_frame = ttk.LabelFrame(controls_frame, text=" Live PBR Material Controls ", padding="10")
        slider_frame.pack(fill=tk.X, pady=4)

        ttk.Label(slider_frame, text="Material Preset:").grid(row=0, column=0, sticky=tk.W)
        self.preset_combo = ttk.Combobox(slider_frame, values=self._preset_combo_values())
        self.preset_combo.grid(row=0, column=1, sticky=tk.EW, padx=5, pady=4)
        self.preset_combo.set("Polished Steel")
        self.preset_combo.bind("<<ComboboxSelected>>", self.on_preset_selected)
        self.preset_combo.bind("<KeyRelease>", self.filter_presets)

        preset_btn_row = ttk.Frame(slider_frame)
        preset_btn_row.grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(0, 4))
        ttk.Button(preset_btn_row, text="Save As New Preset...", command=self.save_current_as_preset).pack(side=tk.LEFT)
        ttk.Button(preset_btn_row, text="Delete Preset", command=self.delete_current_preset).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(preset_btn_row, text="(only custom \"My: ...\" presets can be deleted)",
                  foreground="#888").pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(slider_frame, text="Reflectivity Bias (_r.dds):").grid(row=2, column=0, sticky=tk.W)
        self.metal_slider = ttk.Scale(slider_frame, from_=0, to=255, value=240, command=lambda v: self.on_metal_change())
        self.metal_slider.grid(row=2, column=1, sticky=tk.EW, padx=5, pady=4)
        self.metal_val_lbl = ttk.Label(slider_frame, text="240", width=5)
        self.metal_val_lbl.grid(row=2, column=2)

        ttk.Label(slider_frame, text="Albedo Light Multiplier:").grid(row=3, column=0, sticky=tk.W)
        self.light_slider = ttk.Scale(slider_frame, from_=0.2, to=2.0, value=1.0, command=lambda v: self.on_light_change())
        self.light_slider.grid(row=3, column=1, sticky=tk.EW, padx=5, pady=4)
        self.light_val_lbl = ttk.Label(slider_frame, text="1.00x", width=5)
        self.light_val_lbl.grid(row=3, column=2)

        ttk.Label(slider_frame, text="Reflection Map Strength (_r.dds):").grid(row=4, column=0, sticky=tk.W)
        self.spec_slider = ttk.Scale(slider_frame, from_=0.1, to=3.0, value=1.6, command=lambda v: self.on_spec_change())
        self.spec_slider.grid(row=4, column=1, sticky=tk.EW, padx=5, pady=4)
        self.spec_val_lbl = ttk.Label(slider_frame, text="1.60x", width=5)
        self.spec_val_lbl.grid(row=4, column=2)

        self.white_ao_var = tk.BooleanVar(value=False)
        self.white_ao_chk = ttk.Checkbutton(slider_frame, text="Use Pure White Lightmap AO (_l Green = 255)",
                                             variable=self.white_ao_var, command=self.on_white_ao_toggle)
        self.white_ao_chk.grid(row=5, column=0, columnspan=3, sticky=tk.W, pady=4)

        # NOTE: vanilla FO4 _s.dds is BC5 (R = specular, G = gloss, no blue, no alpha),
        # so there is no alpha env-mask or RGB tint in vanilla files. These two options
        # only matter for custom/modded spec maps that actually carry that data, so
        # they default to OFF. (Tinting a BC5 spec map would read gloss as a color
        # channel and produce a bogus tint.)
        self.env_mask_var = tk.BooleanVar(value=False)
        self.env_mask_chk = ttk.Checkbutton(
            slider_frame,
            text="Apply Env Mask from _s.dds Alpha (custom spec maps only)",
            variable=self.env_mask_var, command=self.on_env_mask_toggle
        )
        self.env_mask_chk.grid(row=9, column=0, columnspan=3, sticky=tk.W, pady=4)

        self.spec_tint_var = tk.BooleanVar(value=False)
        self.spec_tint_chk = ttk.Checkbutton(
            slider_frame,
            text="Tint Reflection with _s.dds RGB (custom spec maps only)",
            variable=self.spec_tint_var, command=self.on_spec_tint_toggle
        )
        self.spec_tint_chk.grid(row=10, column=0, columnspan=3, sticky=tk.W, pady=4)

        ttk.Label(slider_frame, text="Subsurface Scattering (_l.dds Blue):").grid(row=6, column=0, sticky=tk.W)
        self.sss_slider = ttk.Scale(slider_frame, from_=0, to=255, value=0, command=lambda v: self.on_sss_change())
        self.sss_slider.grid(row=6, column=1, sticky=tk.EW, padx=5, pady=4)
        self.sss_val_lbl = ttk.Label(slider_frame, text="0", width=5)
        self.sss_val_lbl.grid(row=6, column=2)

        self.bc7_diffuse_var = tk.BooleanVar(value=self.settings.get("bc7_diffuse", False))
        self.bc7_diffuse_chk = ttk.Checkbutton(
            slider_frame,
            text="Use BC7 (sRGB - DX11) for Diffuse (_d.dds) [Higher Quality, Larger File]",
            variable=self.bc7_diffuse_var,
            command=self._save_bc7_toggles
        )
        self.bc7_diffuse_chk.grid(row=7, column=0, columnspan=3, sticky=tk.W, pady=4)

        self.bc7_reflection_var = tk.BooleanVar(value=self.settings.get("bc7_reflection", False))
        self.bc7_reflection_chk = ttk.Checkbutton(
            slider_frame,
            text="Use BC7 (sRGB - DX11) for Reflection (_r.dds) [Higher Quality, Larger File]",
            variable=self.bc7_reflection_var,
            command=self._save_bc7_toggles
        )
        self.bc7_reflection_chk.grid(row=8, column=0, columnspan=3, sticky=tk.W, pady=4)

        slider_frame.columnconfigure(1, weight=1)

        # 3. OUTPUT SELECTION - which texture types to actually generate/save
        output_frame = ttk.LabelFrame(controls_frame, text=" Output Selection (which files to generate) ", padding="8")
        output_frame.pack(fill=tk.X, pady=4)

        self.gen_diffuse_var = tk.BooleanVar(value=self.settings.get("gen_diffuse", True))
        self.gen_normal_var = tk.BooleanVar(value=self.settings.get("gen_normal", True))
        self.gen_reflection_var = tk.BooleanVar(value=self.settings.get("gen_reflection", True))
        self.gen_lightmap_var = tk.BooleanVar(value=self.settings.get("gen_lightmap", True))

        ttk.Checkbutton(output_frame, text="Diffuse (_d.dds)", variable=self.gen_diffuse_var,
                         command=self._save_output_toggles).grid(row=0, column=0, sticky=tk.W, padx=4)
        ttk.Checkbutton(output_frame, text="Normal (_n.dds)", variable=self.gen_normal_var,
                         command=self._save_output_toggles).grid(row=0, column=1, sticky=tk.W, padx=4)
        ttk.Checkbutton(output_frame, text="Reflection (_r.dds)", variable=self.gen_reflection_var,
                         command=self._save_output_toggles).grid(row=1, column=0, sticky=tk.W, padx=4)
        ttk.Checkbutton(output_frame, text="Lightmap (_l.dds)", variable=self.gen_lightmap_var,
                         command=self._save_output_toggles).grid(row=1, column=1, sticky=tk.W, padx=4)
        ttk.Label(output_frame, text="Unchecked types are skipped entirely on export (faster) - "
                                      "this choice is remembered between sessions.",
                  foreground="#888", wraplength=420).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(4, 0))

        # 4. PERFORMANCE - parallel vs one-at-a-time encoding
        perf_frame = ttk.LabelFrame(controls_frame, text=" Performance ", padding="8")
        perf_frame.pack(fill=tk.X, pady=4)

        self.parallel_var = tk.BooleanVar(value=self.settings.get("parallel_encoding", True))
        self.worker_choice_var = tk.StringVar(value=self.settings.get("worker_choice", "Auto"))

        ttk.Checkbutton(perf_frame, text="Encode multiple textures in parallel (uses more CPU, faster on batches)",
                         variable=self.parallel_var, command=self._on_parallel_toggle).grid(
            row=0, column=0, columnspan=3, sticky=tk.W)

        ttk.Label(perf_frame, text="Parallel workers:").grid(row=1, column=0, sticky=tk.W, padx=(20, 4), pady=(4, 0))
        self.worker_combo = ttk.Combobox(perf_frame, textvariable=self.worker_choice_var, state="readonly",
                                          width=8, values=["Auto", "1", "2", "3", "4", "6", "8", "12", "16"])
        self.worker_combo.grid(row=1, column=1, sticky=tk.W, pady=(4, 0))
        self.worker_combo.bind("<<ComboboxSelected>>", lambda e: self._save_perf_settings())

        detected_cores = os.cpu_count() or 4
        ttk.Label(perf_frame, text=f"(this machine has {detected_cores} CPU core(s) - Auto uses up to 8)",
                  foreground="#888").grid(row=1, column=2, sticky=tk.W, padx=(8, 0), pady=(4, 0))

        ttk.Label(perf_frame, text="Turn this off, or set workers to 1, to encode one file at a time "
                                    "if you're on a slower/shared machine.",
                  foreground="#888", wraplength=420).grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))

        self.worker_combo.config(state="readonly" if self.parallel_var.get() else "disabled")

        # 5. CONSOLE LOG - its own pane so it can be resized independently of
        # the controls above it by dragging the splitter, and grows/shrinks
        # properly when the window itself is resized or maximized.
        log_frame = ttk.LabelFrame(left_paned, text=" Console Log ", padding="5")
        left_paned.add(log_frame, weight=2)

        self.log_widget = scrolledtext.ScrolledText(log_frame, state='disabled', wrap=tk.WORD, height=12)
        self.log_widget.pack(fill=tk.BOTH, expand=True)

        # 6. TEXTURE SET PREVIEW & EDITING PANEL
        set_row = ttk.Frame(right_panel)
        set_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(set_row, text="Texture Set:").pack(side=tk.LEFT)
        self.set_combo = ttk.Combobox(set_row, state="readonly", width=22)
        self.set_combo.pack(side=tk.LEFT, padx=4)
        self.set_combo.bind("<<ComboboxSelected>>", self.on_set_selected)
        ttk.Button(set_row, text="Rescan", command=self.refresh_texture_sets).pack(side=tk.LEFT)

        view_row = ttk.Frame(right_panel)
        view_row.pack(fill=tk.X, pady=(0, 6))
        self.view_mode = tk.StringVar(value="flat")
        ttk.Radiobutton(view_row, text="Flat Texture", variable=self.view_mode, value="flat",
                         command=self.on_view_mode_change).pack(side=tk.LEFT)
        ttk.Radiobutton(view_row, text="Material Ball", variable=self.view_mode, value="ball",
                         command=self.on_view_mode_change).pack(side=tk.LEFT)

        preview_container = ttk.Frame(right_panel)
        preview_container.pack()

        self.flat_frame = ttk.Frame(preview_container)
        self.flat_frame.pack()

        self.preview_kinds = ["diffuse", "normal", "reflection", "lightmap"]
        for idx, kind in enumerate(self.preview_kinds):
            r, c = divmod(idx, 2)
            tile = ttk.Frame(self.flat_frame, padding=4, relief=tk.GROOVE, borderwidth=1)
            tile.grid(row=r, column=c, padx=4, pady=4)
            ttk.Label(tile, text=MAP_LABELS[kind]).pack()
            cv = tk.Canvas(tile, width=140, height=140, bg="#18191c", highlightthickness=0)
            cv.pack()
            self.preview_canvases[kind] = cv
            ttk.Button(tile, text="Edit...", command=lambda k=kind: self.open_editor(k)).pack(fill=tk.X, pady=(4, 0))

        self.ball_frame = ttk.Frame(preview_container)
        self.ball_canvas = tk.Canvas(self.ball_frame, width=220, height=220, bg="#18191c", highlightthickness=0)
        self.ball_canvas.pack()
        ttk.Label(self.ball_frame, text="Combined look using the current sliders and the\n"
                                         "texture set's average diffuse color.",
                  foreground="#888", justify=tk.CENTER).pack(pady=(6, 0))
        # ball_frame starts hidden - flat_frame is the default view

        color_frame = ttk.Frame(right_panel)
        color_frame.pack(fill=tk.X, pady=(10, 4))
        ttk.Label(color_frame, text="Default Brush Color:").pack(side=tk.LEFT, padx=2)
        self.brush_color_swatch = tk.Canvas(color_frame, width=24, height=18, bg=self._hex(self.default_brush_color),
                                             relief=tk.SUNKEN, bd=1)
        self.brush_color_swatch.pack(side=tk.LEFT, padx=4)
        ttk.Button(color_frame, text="Pick Color", command=self.pick_default_brush_color).pack(side=tk.LEFT, padx=2)

        palette_frame = ttk.Frame(right_panel)
        palette_frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(palette_frame, text="Saved Colors (right-click to remove):").pack(anchor=tk.W, padx=2)
        self.custom_color_swatch_frame = ttk.Frame(palette_frame)
        self.custom_color_swatch_frame.pack(fill=tk.X, padx=2, pady=(2, 0))
        self._refresh_custom_color_swatches()

        btn_run = ttk.Button(right_panel, text="▶ Convert Textures", command=self.start_conversion)
        btn_run.pack(fill=tk.X, pady=(16, 5))

    @staticmethod
    def _hex(rgb):
        return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

    # Input path / set scanning
    def check_and_update_input_path(self):
        raw_path = self.input_entry.get().strip().strip('"')
        if raw_path and raw_path != self.last_checked_input_path:
            p = Path(raw_path)
            if p.exists() and p.is_dir():
                self.last_checked_input_path = raw_path
                diff_files = list(p.rglob("*_d.dds"))
                if diff_files:
                    avg_col = get_average_color(diff_files[0])
                    self.set_default_brush_color(avg_col)
                    self.log(f"[Info] Auto-detected texture color: {diff_files[0].name} -> RGB{avg_col}")
                self.refresh_texture_sets()

    def refresh_texture_sets(self):
        raw_path = self.input_entry.get().strip().strip('"')
        if not raw_path:
            return
        in_dir = Path(raw_path)
        if not in_dir.exists() or not in_dir.is_dir():
            return

        self.available_sets = {}
        self.set_materials = {}
        if self.discover_var.get():
            if self.bgsm_materials:
                found, skipped = discover_texture_sets(self.bgsm_materials, in_dir, self._materials_root)
                for st in found:
                    self.available_sets[st["key"]] = (st["diffuse"], st["normal"], st["spec"])
                    self.set_materials[st["key"]] = (st["info"], st["fp"])
                note = f"; {len(skipped)} material(s) skipped (diffuse not in Input Dir)" if skipped else ""
                self.log(f"[Info] Found {len(found)} texture set(s) from the .bgsm files{note}")
                for sk in skipped[:3]:
                    self.log(f"  - skipped {sk}")
            else:
                self.log("[Warning] Find-sets-from-.bgsm is on, but no .bgsm files are loaded - "
                         "set the Materials Dir.")
        else:
            for diff_file in sorted(in_dir.rglob("*_d.dds")):
                norm_file, spec_file = find_set_files(diff_file)
                key = get_set_key(diff_file, in_dir)
                self.available_sets[key] = (diff_file, norm_file, spec_file)

        keys = list(self.available_sets.keys())
        self.set_combo["values"] = keys
        if keys:
            if self.current_set_key not in self.available_sets:
                self.current_set_key = keys[0]
            self.set_combo.set(self.current_set_key)
        else:
            self.current_set_key = None
            self.set_combo.set("")
        self.preview_cache = None
        self._refresh_material_info()
        self.update_active_preview()

    def on_set_selected(self, event=None):
        self.current_set_key = self.set_combo.get()
        self.preview_cache = None
        self._refresh_material_info()
        self.update_active_preview()

    # Pipeline params / preview
    def get_current_pipeline_params(self, apply_material=True):
        params = dict(
            metal_offset=int(self.metal_slider.get()) - 180,
            light_mult=float(self.light_slider.get()),
            reflection_boost=float(self.spec_slider.get()),
            use_white_ao=self.white_ao_var.get(),
            sss_strength=int(self.sss_slider.get()),
            use_env_mask=self.env_mask_var.get(),
            use_spec_tint=self.spec_tint_var.get(),
            tint_rgb=None,
            gate_bias_by_spec=False,
        )
        # For the selected set (preview), swap in the values its .bgsm implies. Batch export
        # resolves this per set instead (apply_material=False + run_batch).
        if (apply_material and self.auto_material_var.get() and self.bgsm_index
                and self.current_set_key in self.available_sets):
            overrides, _msg = self._material_for_set(self.current_set_key)
            params.update(overrides)
        return params

    def _build_current_set_images_full_res(self):

        if not self.current_set_key or self.current_set_key not in self.available_sets:
            return None
        diff_file, norm_file, spec_file = self.available_sets[self.current_set_key]
        params = self.get_current_pipeline_params()
        images, has_alpha, target_size = build_fo76_images(diff_file, norm_file, spec_file, **params)
        return images

    def _get_preview_cache(self):
        if self.preview_cache is None and self.current_set_key in self.available_sets:
            diff_file, norm_file, spec_file = self.available_sets[self.current_set_key]
            self.preview_cache = SetPreviewCache(diff_file, norm_file, spec_file)
        return self.preview_cache

    def update_active_preview(self, changed_maps=None):
        if self.view_mode.get() == "ball":
            self.update_ball_preview()
        else:
            self.update_texture_previews(changed_maps)

    def on_view_mode_change(self):
        if self.view_mode.get() == "flat":
            self.ball_frame.pack_forget()
            self.flat_frame.pack()
        else:
            self.flat_frame.pack_forget()
            self.ball_frame.pack()
        self.update_active_preview()

    def update_ball_preview(self):
        if not self.current_set_key or self.current_set_key not in self.available_sets:
            self.ball_canvas.delete("all")
            self.ball_canvas.create_text(110, 110, text="No texture set selected", fill="#666")
            return

        cache = self._get_preview_cache()
        base_color = cache.get_average_diffuse_color()
        params = self.get_current_pipeline_params()
        metal_raw = min(255, max(0, int(params["metal_offset"]) + 180))

        img = self.sphere_renderer.render(
            smoothness=metal_raw, metalness=metal_raw, base_color=base_color,
            light_mult=params["light_mult"], spec_boost=params["reflection_boost"]
        )
        self.ball_photo = ImageTk.PhotoImage(img)
        self.ball_canvas.delete("all")
        self.ball_canvas.create_image(0, 0, image=self.ball_photo, anchor=tk.NW)

    def update_texture_previews(self, changed_maps=None):
        if not self.current_set_key or self.current_set_key not in self.available_sets:
            for kind in self.preview_kinds:
                cv = self.preview_canvases[kind]
                cv.delete("all")
                cv.create_text(70, 70, text="N/A", fill="#666")
            return

        cache = self._get_preview_cache()
        params = self.get_current_pipeline_params()
        edits_for_set = self.texture_edits.get(self.current_set_key, {})

        kinds = self.preview_kinds if changed_maps is None else [k for k in self.preview_kinds if k in changed_maps]

        for kind in kinds:
            if kind == "diffuse":
                img = cache.get_diffuse(params["light_mult"])
            elif kind == "normal":
                img = cache.get_normal()
            elif kind == "reflection":
                img = cache.get_reflection(params["metal_offset"], params["reflection_boost"],
                                            params["use_env_mask"], params["use_spec_tint"],
                                            params["tint_rgb"], params["gate_bias_by_spec"])
            elif kind == "lightmap":
                img = cache.get_lightmap(params["use_white_ao"], params["sss_strength"])
            else:
                img = None
            self._render_preview_tile(kind, img, edits_for_set.get(kind))

    def _render_preview_tile(self, kind, img, edit_state):
        cv = self.preview_canvases[kind]
        cv.delete("all")
        if img is None:
            cv.create_text(70, 70, text="N/A", fill="#666")
            return

        display_img = edit_state.apply(img) if edit_state is not None else img

        if kind == "diffuse":
            thumb_bg = Image.new("RGB", (140, 140), (60, 60, 60))
            thumb_rgba = display_img.copy()
            thumb_rgba.thumbnail((140, 140), Image.Resampling.LANCZOS)
            paste_x = (140 - thumb_rgba.width) // 2
            paste_y = (140 - thumb_rgba.height) // 2
            thumb_bg.paste(thumb_rgba.convert("RGB"), (paste_x, paste_y), thumb_rgba.getchannel("A"))
            thumb = thumb_bg
        else:
            thumb = display_img.convert("RGB")
            thumb.thumbnail((140, 140), Image.Resampling.LANCZOS)

        photo = ImageTk.PhotoImage(thumb)
        self.preview_photo_refs[kind] = photo
        cv.create_image(70, 70, image=photo)

    # Debounced, targeted updates
    def _mark_dirty(self, maps):

        self._dirty_maps.update(maps)
        if self._debounce_job is not None:
            self.root.after_cancel(self._debounce_job)
        self._debounce_job = self.root.after(80, self._flush_dirty)

    def _flush_dirty(self):
        self._debounce_job = None
        maps = self._dirty_maps
        self._dirty_maps = set()
        if maps:
            self.update_active_preview(maps)


    # Each control only marks the map(s) it actually feeds into as dirty, so e.g.
    # dragging the Light Multiplier slider no longer touches Normal/Reflection/Lightmap.
    def on_metal_change(self):
        self.metal_val_lbl.config(text=f"{int(self.metal_slider.get())}")
        self._mark_dirty({"reflection"})

    def on_light_change(self):
        self.light_val_lbl.config(text=f"{float(self.light_slider.get()):.2f}x")
        self._mark_dirty({"diffuse"})

    def on_spec_change(self):
        self.spec_val_lbl.config(text=f"{float(self.spec_slider.get()):.2f}x")
        self._mark_dirty({"reflection"})

    def on_sss_change(self):
        self.sss_val_lbl.config(text=f"{int(self.sss_slider.get())}")
        self._mark_dirty({"lightmap"})

    def on_white_ao_toggle(self):
        self._mark_dirty({"lightmap"})

    def on_env_mask_toggle(self):
        self._mark_dirty({"reflection"})

    def on_spec_tint_toggle(self):
        self._mark_dirty({"reflection"})

    # Editor
    def open_editor(self, kind):
        if not self.current_set_key:
            messagebox.showinfo("No Texture Set", "Select an input directory with a texture set first.")
            return

        images = self._build_current_set_images_full_res()
        base_img = images.get(kind) if images else None
        if base_img is None:
            messagebox.showinfo("Not Available", f"The {MAP_LABELS.get(kind, kind)} map is not "
                                 "available for this texture set (e.g. missing source file).")
            return

        edits_for_set = self.texture_edits.setdefault(self.current_set_key, {})
        edit_state = edits_for_set.get(kind)
        if edit_state is None or edit_state.size != base_img.size:
            edit_state = TextureEditState(base_img.size)
            edits_for_set[kind] = edit_state

        dialog = TextureEditorDialog(
            self.root,
            f"Edit {MAP_LABELS.get(kind, kind)} — {self.current_set_key}",
            base_img, edit_state, kind,
            default_color=self.default_brush_color,
            palette_get=self.get_custom_colors,
            palette_add=self.add_custom_color,
            palette_remove=self.remove_custom_color,
        )
        self.root.wait_window(dialog)
        self.update_active_preview()

    # Brush color / presets
    def set_default_brush_color(self, rgb_tuple):
        self.default_brush_color = tuple(int(c) for c in rgb_tuple)
        self.brush_color_swatch.config(bg=self._hex(self.default_brush_color))
        self.settings["default_brush_color"] = list(self.default_brush_color)
        save_app_settings(self.settings)

    def pick_default_brush_color(self):
        color_code = colorchooser.askcolor(title="Choose Default Brush Color", initialcolor=self._hex(self.default_brush_color))
        if color_code and color_code[0]:
            r, g, b = [int(c) for c in color_code[0]]
            self.set_default_brush_color((r, g, b))
            self.add_custom_color((r, g, b))

    # Custom color palette (persisted)
    def get_custom_colors(self):
        return list(self.custom_colors)

    def add_custom_color(self, rgb):
        rgb = tuple(int(c) for c in rgb)
        if rgb in self.custom_colors:
            self.custom_colors.remove(rgb)
        self.custom_colors.insert(0, rgb)
        self.custom_colors = self.custom_colors[:12]
        self.settings["custom_colors"] = [list(c) for c in self.custom_colors]
        save_app_settings(self.settings)
        self._refresh_custom_color_swatches()

    def remove_custom_color(self, rgb):
        rgb = tuple(int(c) for c in rgb)
        if rgb in self.custom_colors:
            self.custom_colors.remove(rgb)
            self.settings["custom_colors"] = [list(c) for c in self.custom_colors]
            save_app_settings(self.settings)
            self._refresh_custom_color_swatches()

    def _refresh_custom_color_swatches(self):
        for w in self.custom_color_swatch_frame.winfo_children():
            w.destroy()
        if not self.custom_colors:
            ttk.Label(self.custom_color_swatch_frame, text="(none yet - colors you pick are saved here)",
                      foreground="#666").pack(side=tk.LEFT)
            return
        for rgb in self.custom_colors:
            b = tk.Button(self.custom_color_swatch_frame, bg=self._hex(rgb), width=2, height=1,
                          relief=tk.RAISED, command=lambda c=rgb: self.set_default_brush_color(c))
            b.bind("<Button-3>", lambda e, c=rgb: self.remove_custom_color(c))
            b.pack(side=tk.LEFT, padx=1, pady=1)

    # Output toggle persistence
    def _save_output_toggles(self):
        self.settings["gen_diffuse"] = self.gen_diffuse_var.get()
        self.settings["gen_normal"] = self.gen_normal_var.get()
        self.settings["gen_reflection"] = self.gen_reflection_var.get()
        self.settings["gen_lightmap"] = self.gen_lightmap_var.get()
        save_app_settings(self.settings)

    # BC7 toggle persistence
    def _save_bc7_toggles(self):
        self.settings["bc7_diffuse"] = self.bc7_diffuse_var.get()
        self.settings["bc7_reflection"] = self.bc7_reflection_var.get()
        save_app_settings(self.settings)

    # Performance (parallel encoding)
    def _on_parallel_toggle(self):
        self.worker_combo.config(state="readonly" if self.parallel_var.get() else "disabled")
        self._save_perf_settings()

    def _save_perf_settings(self):
        self.settings["parallel_encoding"] = self.parallel_var.get()
        self.settings["worker_choice"] = self.worker_choice_var.get()
        save_app_settings(self.settings)

    def _resolve_worker_count(self):

        if not self.parallel_var.get():
            return 1
        choice = self.worker_choice_var.get()
        if choice == "Auto":
            return min(8, max(2, os.cpu_count() or 4))
        try:
            return max(1, int(choice))
        except ValueError:
            return min(8, max(2, os.cpu_count() or 4))

    def _preset_combo_values(self):
        builtin = list(MATERIAL_PRESETS.keys())
        custom = [f"{CUSTOM_PRESET_PREFIX}{name}" for name in sorted(self.custom_presets.keys())]
        return builtin + custom

    def filter_presets(self, event):
        typed = self.preset_combo.get().lower()
        all_values = self._preset_combo_values()
        if not typed:
            self.preset_combo['values'] = all_values
        else:
            self.preset_combo['values'] = [k for k in all_values if typed in k.lower()]

    def on_preset_selected(self, event=None):
        preset_name = self.preset_combo.get()

        if preset_name.startswith(CUSTOM_PRESET_PREFIX):
            real_name = preset_name[len(CUSTOM_PRESET_PREFIX):]
            data = self.custom_presets.get(real_name)
            if data is None:
                return
            metal = data.get("metal", 240)
            light = data.get("light", 1.0)
            spec = data.get("spec", 1.6)
            white_ao = data.get("white_ao")
            sss = data.get("sss")
            color = None
        elif preset_name in MATERIAL_PRESETS:
            metal, light, spec, color = MATERIAL_PRESETS[preset_name]
            white_ao = None
            sss = None
        else:
            return

        self.metal_slider.set(metal)
        self.light_slider.set(light)
        self.spec_slider.set(spec)
        self.metal_val_lbl.config(text=f"{int(metal)}")
        self.light_val_lbl.config(text=f"{float(light):.2f}x")
        self.spec_val_lbl.config(text=f"{float(spec):.2f}x")
        if white_ao is not None:
            self.white_ao_var.set(bool(white_ao))
        if sss is not None:
            self.sss_slider.set(sss)
            self.sss_val_lbl.config(text=f"{int(sss)}")
        if color is not None and not self.last_checked_input_path:
            self.set_default_brush_color(color)

        if self._debounce_job is not None:
            self.root.after_cancel(self._debounce_job)
            self._debounce_job = None
        self._dirty_maps = set()
        self.update_active_preview()

    def save_current_as_preset(self):
        name = simpledialog.askstring("Save Preset", "Name this preset:", parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if name in MATERIAL_PRESETS:
            messagebox.showerror("Error", f'"{name}" is a built-in preset name and can\'t be used - '
                                  "choose a different name for your custom preset.")
            return

        overwrite = name in self.custom_presets
        self.custom_presets[name] = {
            "metal": int(self.metal_slider.get()),
            "light": float(self.light_slider.get()),
            "spec": float(self.spec_slider.get()),
            "white_ao": bool(self.white_ao_var.get()),
            "sss": int(self.sss_slider.get()),
        }
        self.settings["custom_presets"] = self.custom_presets
        save_app_settings(self.settings)

        self.preset_combo['values'] = self._preset_combo_values()
        self.preset_combo.set(f"{CUSTOM_PRESET_PREFIX}{name}")
        self.log(f"[Info] {'Updated' if overwrite else 'Saved'} custom preset '{name}'.")

    def delete_current_preset(self):
        preset_name = self.preset_combo.get()
        if not preset_name.startswith(CUSTOM_PRESET_PREFIX):
            messagebox.showinfo("Delete Preset", "Only custom presets (shown as \"My: ...\") can be "
                                 "deleted - built-in presets are permanent.")
            return
        real_name = preset_name[len(CUSTOM_PRESET_PREFIX):]
        if real_name not in self.custom_presets:
            return
        if not messagebox.askyesno("Delete Preset", f'Delete the custom preset "{real_name}"? '
                                    "This can't be undone."):
            return
        del self.custom_presets[real_name]
        self.settings["custom_presets"] = self.custom_presets
        save_app_settings(self.settings)
        self.preset_combo['values'] = self._preset_combo_values()
        self.preset_combo.set("")
        self.log(f"[Info] Deleted custom preset '{real_name}'.")

    # Material (.bgsm) auto-detection
    def browse_materials(self):
        path = filedialog.askdirectory(title="Select Materials Directory (contains .bgsm files)")
        if path:
            self.materials_entry.delete(0, tk.END)
            self.materials_entry.insert(0, path)
            self._on_materials_entry_changed()

    def _on_materials_entry_changed(self):
        raw = self.materials_entry.get().strip().strip('"')
        if raw == self._last_materials_dir:
            return
        self.settings["materials_dir"] = raw
        save_app_settings(self.settings)
        self._rebuild_bgsm_index()
        self.update_active_preview()

    def _rebuild_bgsm_index(self):
        raw = self.materials_entry.get().strip().strip('"')
        self._last_materials_dir = raw
        self.bgsm_index = {}
        self.bgsm_materials = []
        self._materials_root = None
        if raw:
            p = Path(raw)
            if not p.is_dir():
                self.log(f"[Warning] Materials directory not found: {raw}")
            else:
                materials, errors = load_bgsm_materials(p)
                index = index_bgsm_materials(materials)
                loaded = len(materials)
                self.bgsm_index = index
                self.bgsm_materials = materials
                self._materials_root = p
                note = f" ({len(errors)} skipped)" if errors else ""
                self.log(f"[Info] Loaded {loaded} material file(s) from {raw}{note}")
                samples = sorted({k for lst in index.values() for k, _i, _f in lst})[:4]
                if samples:
                    self.log("[Info] Texture sets referenced by those materials, e.g.: " + ", ".join(samples))
                for e in errors[:5]:
                    self.log(f"  - skipped {e}")
                if len(errors) > 5:
                    self.log(f"  - ...and {len(errors) - 5} more skipped")
        self._refresh_material_info()
        if self.discover_var.get():
            self.refresh_texture_sets()

    def on_auto_material_toggle(self):
        self.settings["auto_material"] = self.auto_material_var.get()
        save_app_settings(self.settings)
        if self.auto_material_var.get():
            self._on_materials_entry_changed()
            if not self.bgsm_index:
                self.log("[Warning] Pick-material-from-.bgsm is on, but no .bgsm files are loaded - "
                         "set the Materials Dir.")
        self._refresh_material_info()
        self.update_active_preview()

    def on_discover_toggle(self):
        self.settings["discover_from_bgsm"] = self.discover_var.get()
        save_app_settings(self.settings)
        if self.discover_var.get():
            self._on_materials_entry_changed()
        self.refresh_texture_sets()

    def _material_for_set(self, set_key):
        # (overrides, message) for a texture set: its own .bgsm when sets come from the
        # materials, otherwise looked up by texture path.
        if set_key in self.set_materials:
            info, fp = self.set_materials[set_key]
            return resolve_material_for_info(info, fp)
        if self.bgsm_index and set_key in self.available_sets:
            return resolve_material_for_set(self.bgsm_index, self.available_sets[set_key][0])
        return {}, "no materials loaded"

    def _refresh_material_info(self):
        text = ""
        if self.auto_material_var.get():
            if not self.bgsm_index:
                text = "No .bgsm files loaded - set the Materials Dir."
            elif self.current_set_key in self.available_sets:
                overrides, msg = self._material_for_set(self.current_set_key)
                text = f"Selected set: {msg}"
                if overrides:
                    text += "  (preset/sliders overridden for this set)"
        self.material_info_lbl.config(text=text)

    # FO76 .bgsm output
    def browse_fo76_materials(self):
        path = filedialog.askdirectory(title="Select folder to write FO76 .bgsm files into")
        if path:
            self.fo76_mat_entry.delete(0, tk.END)
            self.fo76_mat_entry.insert(0, path)
            self._save_bgsm_output_settings()

    def _save_bgsm_output_settings(self):
        self.settings["fo76_materials_dir"] = self.fo76_mat_entry.get().strip().strip('"')
        self.settings["create_fo76_bgsm"] = self.create_bgsm_var.get()
        save_app_settings(self.settings)

    # Browsing / logging
    def browse_input(self):
        path = filedialog.askdirectory(title="Select Input Directory")
        if path:
            self.input_entry.delete(0, tk.END)
            self.input_entry.insert(0, path)
            self.check_and_update_input_path()

    def browse_output(self):
        path = filedialog.askdirectory(title="Select Output Directory")
        if path:
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, path)

    def log(self, message):
        # Safe to call from any thread (batch export now uses a worker pool) -
        # the actual widget update always runs on the Tk main loop.
        self.root.after(0, self._append_log, message)

    def _append_log(self, message):
        self.log_widget.config(state='normal')
        self.log_widget.insert(tk.END, message + "\n")
        self.log_widget.see(tk.END)
        self.log_widget.config(state='disabled')

    # Conversion
    def start_conversion(self):
        in_dir = self.input_entry.get().strip().strip('"')
        out_dir = self.output_entry.get().strip().strip('"')

        if not in_dir or not Path(in_dir).exists():
            messagebox.showerror("Error", "Please select a valid input directory.")
            return
        if not out_dir:
            messagebox.showerror("Error", "Please select an output directory.")
            return

        enabled_maps = set()
        if self.gen_diffuse_var.get():
            enabled_maps.add("diffuse")
        if self.gen_normal_var.get():
            enabled_maps.add("normal")
        if self.gen_reflection_var.get():
            enabled_maps.add("reflection")
        if self.gen_lightmap_var.get():
            enabled_maps.add("lightmap")

        if not enabled_maps:
            messagebox.showerror("Error", "At least one output type must be checked in "
                                  "Output Selection (Diffuse/Normal/Reflection/Lightmap).")
            return

        params = self.get_current_pipeline_params(apply_material=False)
        uses_materials = (self.auto_material_var.get() or self.discover_var.get()
                          or self.create_bgsm_var.get())
        if uses_materials and self.materials_entry.get().strip():
            self._rebuild_bgsm_index()      # pick up any files added since it was last loaded

        bgsm_index = None
        if self.auto_material_var.get():
            if not self.bgsm_index:
                messagebox.showerror("Error", "Pick-material-from-.bgsm is on, but no .bgsm files were "
                                              "loaded. Set the Materials Dir, or turn the option off.")
                return
            bgsm_index = self.bgsm_index

        discovery_materials = None
        if self.discover_var.get():
            if not self.bgsm_materials:
                messagebox.showerror("Error", "Find-texture-sets-from-.bgsm is on, but no .bgsm files were "
                                              "loaded. Set the Materials Dir, or turn the option off.")
                return
            discovery_materials = list(self.bgsm_materials)

        bgsm_out = None
        fo4_index = None
        if self.create_bgsm_var.get():
            raw_mat_out = self.fo76_mat_entry.get().strip().strip('"')
            if not raw_mat_out:
                messagebox.showerror("Error", "\"Create an FO76 .bgsm\" is on, but no FO76 Materials Out "
                                              "folder is set.")
                return
            bgsm_out = Path(raw_mat_out)
            self._save_bgsm_output_settings()
            fo4_index = self.bgsm_index or None     # source of FO4 surface flags, if loaded
        use_bc7_diffuse = self.bc7_diffuse_var.get()
        use_bc7_reflection = self.bc7_reflection_var.get()
        worker_count = self._resolve_worker_count()

        threading.Thread(
            target=self.run_batch,
            args=(Path(in_dir), Path(out_dir), params, use_bc7_diffuse, use_bc7_reflection,
                  enabled_maps, worker_count, bgsm_index, bgsm_out, fo4_index,
                  discovery_materials, self._materials_root),
            daemon=True
        ).start()

    def run_batch(self, in_dir, out_dir, params, use_bc7_diffuse, use_bc7_reflection, enabled_maps,
                  worker_count, bgsm_index=None, bgsm_out=None, fo4_index=None,
                  discovery_materials=None, materials_root=None):
        self.log("--- Starting FO76 Spec-Compliant Batch Conversion ---")
        self.log(f"[Info] Generating: {', '.join(sorted(enabled_maps)) if enabled_maps else 'nothing selected'}")

        try:
            texconv_path = find_texconv()
            self.log(f"[Info] Using texconv: {texconv_path}")
        except FileNotFoundError as e:
            self.log(f"[Error] {e}")
            return

        # What to convert: (diffuse, normal, spec, set_key, output_base_name, material).
        # Either one set per *_d.dds file, or one per .bgsm whose textures are in Input Dir.
        work = []
        if discovery_materials is not None:
            found, skipped = discover_texture_sets(discovery_materials, in_dir, materials_root)
            if skipped:
                self.log(f"[Info] {len(skipped)} material(s) skipped (diffuse not in Input Dir), e.g. {skipped[0]}")
            for st in found:
                work.append((st["diffuse"], st["normal"], st["spec"], st["key"], st["fp"].stem,
                             (st["info"], st["fp"])))
            empty_msg = "[Warning] None of the loaded .bgsm files has its diffuse texture in Input Dir."
        else:
            for diff_file in sorted(in_dir.rglob("*_d.dds")):
                norm_file, spec_file = find_set_files(diff_file)
                work.append((diff_file, norm_file, spec_file, get_set_key(diff_file, in_dir), None, None))
            empty_msg = "[Warning] No '*_d.dds' texture sets found."

        if not work:
            self.log(empty_msg)
            return

        # Phase 1: prepare every set's images (PIL/numpy - fast, sequential is fine).
        self.log(f"[Info] Preparing {len(work)} texture set(s)...")
        save_jobs = []
        prep_failed = 0
        bgsm_written = bgsm_failed = 0
        planned_outputs = set()
        for diff_file, norm_file, spec_file, set_key, out_base, material in work:
            rel_dir = diff_file.parent.relative_to(in_dir)
            target_dir = out_dir / rel_dir
            label = out_base or diff_file.stem.rsplit('_', 1)[0]

            if out_base:
                tag = (str(target_dir).lower(), out_base.lower())
                if tag in planned_outputs:
                    prep_failed += 1
                    self.log(f"[Error] Skipped {label}: another material already writes "
                             f"'{out_base}_*.dds' into {target_dir}")
                    continue
                planned_outputs.add(tag)

            edits_for_set = self.texture_edits.get(set_key, {})

            set_params = dict(params)
            if bgsm_index:
                if material:
                    overrides, msg = resolve_material_for_info(*material)
                else:
                    overrides, msg = resolve_material_for_set(bgsm_index, diff_file)
                set_params.update(overrides)
                self.log(f"  [Material] {label}: {msg}")

            try:
                jobs = prepare_texture_set_save_jobs(
                    diff_file, norm_file, spec_file, target_dir,
                    use_bc7_diffuse=use_bc7_diffuse,
                    use_bc7_reflection=use_bc7_reflection,
                    texture_edits=edits_for_set,
                    enabled_maps=enabled_maps,
                    output_base_name=out_base,
                    **set_params
                )
                if not jobs:
                    self.log(f"  - Skipped {label}: no output types selected")
                save_jobs.extend(jobs)

                if bgsm_out is not None and jobs:
                    try:
                        fo4_header = None
                        if material:
                            fo4_header = material[0].header
                        elif fo4_index:
                            fo4_matches, _note = find_bgsm_matches(fo4_index, diff_file)
                            if fo4_matches:
                                fo4_header = fo4_matches[0][0].header
                        mat_path = write_fo76_bgsm(jobs, bgsm_out / rel_dir, out_dir, fo4_header)
                        bgsm_written += 1
                        src = "flags from FO4 .bgsm" if fo4_header else "default flags"
                        self.log(f"  ✓ {mat_path.name} (FO76 material, {src}) -> "
                                 f"{fo76_game_path(jobs[0].out_path, out_dir)}")
                    except Exception as e:
                        bgsm_failed += 1
                        self.log(f"[Error] Failed to write FO76 material for {label}: {e}")
            except Exception as e:
                prep_failed += 1
                self.log(f"[Error] Failed to prepare {label}: {e}")

        if not save_jobs:
            self.log("[Warning] Nothing to encode.")
            return

        # Phase 2: encode the WHOLE batch's DDS files, either one at a time or
        # through a bounded worker pool - the user's choice (Performance panel),
        # since we can't know their CPU/resource constraints in advance. Each
        # texconv call is an independent external process, so running several at
        # once (instead of one at a time) uses more CPU cores and meaningfully
        # speeds up large batches, especially with BC7 enabled - but that's not
        # always what someone wants on a shared or lower-powered machine.
        if worker_count <= 1:
            mode_desc = "one file at a time (parallel encoding is off)"
        else:
            mode_desc = f"{worker_count} parallel worker(s)"
        self.log(f"[Info] Encoding {len(save_jobs)} file(s) across {len(work)} set(s) "
                 f"using {mode_desc}...")

        succeeded, failed = 0, 0

        if worker_count <= 1:
            for job in save_jobs:
                try:
                    save_dds(job.image, job.out_path, job.dxgi_format,
                             is_normal_map=job.is_normal_map, log_func=self.log)
                    succeeded += 1
                    self.log(f"  ✓ {job.out_path.name} {job.label}")
                except Exception as e:
                    failed += 1
                    self.log(f"  ✗ Failed {job.out_path.name}: {e}")
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_to_job = {
                    executor.submit(save_dds, job.image, job.out_path, job.dxgi_format,
                                     is_normal_map=job.is_normal_map, log_func=self.log): job
                    for job in save_jobs
                }
                for future in concurrent.futures.as_completed(future_to_job):
                    job = future_to_job[future]
                    try:
                        future.result()
                        succeeded += 1
                        self.log(f"  ✓ {job.out_path.name} {job.label}")
                    except Exception as e:
                        failed += 1
                        self.log(f"  ✗ Failed {job.out_path.name}: {e}")

        summary = f"\n✔ Batch Conversion Finished! {succeeded} file(s) saved, {failed} failed"
        if prep_failed:
            summary += f", {prep_failed} set(s) failed to prepare"
        if bgsm_out is not None:
            summary += f"; {bgsm_written} .bgsm written"
            if bgsm_failed:
                summary += f", {bgsm_failed} .bgsm failed"
        self.log(summary + ".\n")


if __name__ == "__main__":
    root = tk.Tk()
    app = FO76PBRStudioGUI(root)
    root.mainloop()