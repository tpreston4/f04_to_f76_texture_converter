#!/usr/bin/env python3
# Version: 6.0

import os
import sys
import math
import json
import shutil
import subprocess
import tempfile
import threading
import concurrent.futures
from collections import namedtuple
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, colorchooser

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
    """Locate texconv.exe next to this script, on PATH, or via TEXCONV_PATH."""
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
def process_fo76_reflection_map(
    diffuse_img: Image.Image,
    spec_path: Path,
    target_size,
    reflection_strength: float = 1.6,
    contrast: float = 1.35,
    bias: float = 0.0,
    spec_img: "Image.Image" = None,
) -> "Image.Image":

    if diffuse_img.size != target_size:
        diffuse_img = diffuse_img.resize(target_size, Image.Resampling.LANCZOS)
    diffuse_rgb = diffuse_img.convert("RGB")
    diffuse_arr = np.asarray(diffuse_rgb, dtype=np.float32) / 255.0
    diffuse_luma = (0.299 * diffuse_arr[:, :, 0] +
                    0.587 * diffuse_arr[:, :, 1] +
                    0.114 * diffuse_arr[:, :, 2])

    if spec_img is not None:
        img_s = spec_img
        if img_s.size != target_size:
            img_s = img_s.resize(target_size, Image.Resampling.LANCZOS)
        spec_red = np.asarray(img_s.getchannel("R"), dtype=np.float32) / 255.0
    elif spec_path and spec_path.exists():
        img_s = Image.open(spec_path).convert("RGBA")
        if img_s.size != target_size:
            img_s = img_s.resize(target_size, Image.Resampling.LANCZOS)
        spec_red = np.asarray(img_s.getchannel("R"), dtype=np.float32) / 255.0
    else:
        spec_red = diffuse_luma.copy()

    delta = spec_red - diffuse_luma
    reflect = diffuse_luma + delta * reflection_strength
    reflect = 0.5 + (reflect - 0.5) * contrast
    reflect = reflect + bias
    reflect = np.clip(reflect, 0.0, 1.0)

    reflect_8 = np.clip(reflect * 255.0, 0, 255).astype(np.uint8)
    reflect_img = Image.fromarray(reflect_8, mode="L").convert("RGB")

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
            reflection_strength=reflection_boost, bias=reflectivity_bias
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

    def get_reflection(self, metal_offset: int, reflection_boost: float):
        self.ensure_loaded()
        if self.diffuse_base is None:
            return None
        img_r = process_fo76_reflection_map(
            self.diffuse_base, self.spec_path, self.size,
            reflection_strength=reflection_boost, bias=metal_offset / 255.0,
            spec_img=self._spec_rgba
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
        """For channel-packed maps (normal/reflection/lightmap), zero out any of
        R/G/B that isn't currently checked in the Paint Channels row, so the
        preview shows only the channel(s) you're actually about to paint."""
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
                                  use_bc7_diffuse: bool = False,
                                  use_bc7_reflection: bool = False,
                                  texture_edits: dict = None,
                                  enabled_maps: set = None) -> list:
    """Builds the requested images for one texture set (fast: PIL/numpy only, no
    subprocess calls) and returns a list of SaveJob describing what still needs
    to be encoded to disk. Does NOT call texconv - callers run the jobs (e.g. via
    save_dds), sequentially or in parallel."""
    base_name = diffuse_path.stem.rsplit('_', 1)[0]
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
        label = f"_r [{'BC7 sRGB' if use_bc7_reflection else 'BC1 sRGB'}]"
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
                            use_bc7_diffuse: bool = False,
                            use_bc7_reflection: bool = False,
                            texture_edits: dict = None,
                            enabled_maps: set = None,
                            log_func=print):
    """Convenience wrapper: prepares one texture set's jobs and saves them
    sequentially. Batch export uses prepare_texture_set_save_jobs directly so it
    can encode many sets' jobs in parallel instead - see FO76PBRStudioGUI.run_batch."""
    base_name = diffuse_path.stem.rsplit('_', 1)[0]
    jobs = prepare_texture_set_save_jobs(
        diffuse_path, normal_path, spec_path, output_dir,
        metal_offset, light_mult,
        reflection_boost=reflection_boost, use_white_ao=use_white_ao,
        ao_strength=ao_strength, sss_strength=sss_strength,
        use_bc7_diffuse=use_bc7_diffuse, use_bc7_reflection=use_bc7_reflection,
        texture_edits=texture_edits, enabled_maps=enabled_maps
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
        self.root.geometry("1200x860")
        self.root.minsize(820, 560)
        self.root.resizable(True, True)

        self.last_checked_input_path = ""

        # Persistent settings (custom colors, output toggles, etc) - survives restarts.
        self.settings = load_app_settings()
        self.default_brush_color = tuple(self.settings.get("default_brush_color", (180, 180, 185)))
        self.custom_colors = [tuple(c) for c in self.settings.get("custom_colors", [])]

        # set_key -> (diffuse_path, normal_path, spec_path)
        self.available_sets = {}
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

    def create_widgets(self):
        header = ttk.Frame(self.root, padding="10")
        header.pack(fill=tk.X)
        ttk.Label(header, text="FO76 Material & PBR Calibration Studio", font=("Helvetica", 14, "bold")).pack(anchor=tk.W)
        ttk.Label(header, text="Adjust live metalness, gloss, specular, and base color response, "
                               "then fine-tune each exported texture by hand before batch processing.").pack(anchor=tk.W)

        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Left side: a vertical splitter between the control stack (paths,
        # sliders, output/performance settings) and the console log, so the
        # person can drag to give the log more room instead of being stuck
        # with a fixed, cramped height.
        left_paned = ttk.PanedWindow(main_paned, orient=tk.VERTICAL)
        main_paned.add(left_paned, weight=3)

        controls_frame = ttk.Frame(left_paned)
        left_paned.add(controls_frame, weight=3)

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

        io_frame.columnconfigure(1, weight=1)

        # 2. PBR SLIDERS & PRESETS
        slider_frame = ttk.LabelFrame(controls_frame, text=" Live PBR Material Controls ", padding="10")
        slider_frame.pack(fill=tk.X, pady=4)

        ttk.Label(slider_frame, text="Material Preset:").grid(row=0, column=0, sticky=tk.W)
        self.preset_combo = ttk.Combobox(slider_frame, values=list(MATERIAL_PRESETS.keys()))
        self.preset_combo.grid(row=0, column=1, sticky=tk.EW, padx=5, pady=4)
        self.preset_combo.set("Polished Steel")
        self.preset_combo.bind("<<ComboboxSelected>>", self.on_preset_selected)
        self.preset_combo.bind("<KeyRelease>", self.filter_presets)

        ttk.Label(slider_frame, text="Reflectivity Bias (_r.dds):").grid(row=1, column=0, sticky=tk.W)
        self.metal_slider = ttk.Scale(slider_frame, from_=0, to=255, value=240, command=lambda v: self.on_metal_change())
        self.metal_slider.grid(row=1, column=1, sticky=tk.EW, padx=5, pady=4)
        self.metal_val_lbl = ttk.Label(slider_frame, text="240", width=5)
        self.metal_val_lbl.grid(row=1, column=2)

        ttk.Label(slider_frame, text="Albedo Light Multiplier:").grid(row=2, column=0, sticky=tk.W)
        self.light_slider = ttk.Scale(slider_frame, from_=0.2, to=2.0, value=1.0, command=lambda v: self.on_light_change())
        self.light_slider.grid(row=2, column=1, sticky=tk.EW, padx=5, pady=4)
        self.light_val_lbl = ttk.Label(slider_frame, text="1.00x", width=5)
        self.light_val_lbl.grid(row=2, column=2)

        ttk.Label(slider_frame, text="Reflection Map Strength (_r.dds):").grid(row=3, column=0, sticky=tk.W)
        self.spec_slider = ttk.Scale(slider_frame, from_=0.1, to=3.0, value=1.6, command=lambda v: self.on_spec_change())
        self.spec_slider.grid(row=3, column=1, sticky=tk.EW, padx=5, pady=4)
        self.spec_val_lbl = ttk.Label(slider_frame, text="1.60x", width=5)
        self.spec_val_lbl.grid(row=3, column=2)

        self.white_ao_var = tk.BooleanVar(value=False)
        self.white_ao_chk = ttk.Checkbutton(slider_frame, text="Use Pure White Lightmap AO (_l Green = 255)",
                                             variable=self.white_ao_var, command=self.on_white_ao_toggle)
        self.white_ao_chk.grid(row=4, column=0, columnspan=3, sticky=tk.W, pady=4)

        ttk.Label(slider_frame, text="Subsurface Scattering (_l.dds Blue):").grid(row=5, column=0, sticky=tk.W)
        self.sss_slider = ttk.Scale(slider_frame, from_=0, to=255, value=0, command=lambda v: self.on_sss_change())
        self.sss_slider.grid(row=5, column=1, sticky=tk.EW, padx=5, pady=4)
        self.sss_val_lbl = ttk.Label(slider_frame, text="0", width=5)
        self.sss_val_lbl.grid(row=5, column=2)

        self.bc7_diffuse_var = tk.BooleanVar(value=False)
        self.bc7_diffuse_chk = ttk.Checkbutton(
            slider_frame,
            text="Use BC7 (sRGB - DX11) for Diffuse (_d.dds) [Higher Quality, Larger File]",
            variable=self.bc7_diffuse_var
        )
        self.bc7_diffuse_chk.grid(row=6, column=0, columnspan=3, sticky=tk.W, pady=4)

        self.bc7_reflection_var = tk.BooleanVar(value=False)
        self.bc7_reflection_chk = ttk.Checkbutton(
            slider_frame,
            text="Use BC7 (sRGB - DX11) for Reflection (_r.dds) [Higher Quality, Larger File]",
            variable=self.bc7_reflection_var
        )
        self.bc7_reflection_chk.grid(row=7, column=0, columnspan=3, sticky=tk.W, pady=4)

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
        self.update_active_preview()

    def on_set_selected(self, event=None):
        self.current_set_key = self.set_combo.get()
        self.preview_cache = None
        self.update_active_preview()

    # Pipeline params / preview
    def get_current_pipeline_params(self):
        return dict(
            metal_offset=int(self.metal_slider.get()) - 180,
            light_mult=float(self.light_slider.get()),
            reflection_boost=float(self.spec_slider.get()),
            use_white_ao=self.white_ao_var.get(),
            sss_strength=int(self.sss_slider.get()),
        )

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
        metal_raw = int(self.metal_slider.get())

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
                img = cache.get_reflection(params["metal_offset"], params["reflection_boost"])
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

    # ------------------------------------------------------------- Performance (parallel encoding)
    def _on_parallel_toggle(self):
        self.worker_combo.config(state="readonly" if self.parallel_var.get() else "disabled")
        self._save_perf_settings()

    def _save_perf_settings(self):
        self.settings["parallel_encoding"] = self.parallel_var.get()
        self.settings["worker_choice"] = self.worker_choice_var.get()
        save_app_settings(self.settings)

    def _resolve_worker_count(self):
        """Returns 1 for strictly one-at-a-time encoding, or the number of
        parallel workers to use otherwise."""
        if not self.parallel_var.get():
            return 1
        choice = self.worker_choice_var.get()
        if choice == "Auto":
            return min(8, max(2, os.cpu_count() or 4))
        try:
            return max(1, int(choice))
        except ValueError:
            return min(8, max(2, os.cpu_count() or 4))

    def filter_presets(self, event):
        typed = self.preset_combo.get().lower()
        if not typed:
            self.preset_combo['values'] = list(MATERIAL_PRESETS.keys())
        else:
            filtered = [k for k in MATERIAL_PRESETS.keys() if typed in k.lower()]
            self.preset_combo['values'] = filtered

    def on_preset_selected(self, event=None):
        preset_name = self.preset_combo.get()
        if preset_name in MATERIAL_PRESETS:
            metal, light, spec, color = MATERIAL_PRESETS[preset_name]
            self.metal_slider.set(metal)
            self.light_slider.set(light)
            self.spec_slider.set(spec)
            self.metal_val_lbl.config(text=f"{int(metal)}")
            self.light_val_lbl.config(text=f"{float(light):.2f}x")
            self.spec_val_lbl.config(text=f"{float(spec):.2f}x")
            if not self.last_checked_input_path:
                self.set_default_brush_color(color)

            if self._debounce_job is not None:
                self.root.after_cancel(self._debounce_job)
                self._debounce_job = None
            self._dirty_maps = set()
            self.update_active_preview()

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

        params = self.get_current_pipeline_params()
        use_bc7_diffuse = self.bc7_diffuse_var.get()
        use_bc7_reflection = self.bc7_reflection_var.get()
        worker_count = self._resolve_worker_count()

        threading.Thread(
            target=self.run_batch,
            args=(Path(in_dir), Path(out_dir), params, use_bc7_diffuse, use_bc7_reflection,
                  enabled_maps, worker_count),
            daemon=True
        ).start()

    def run_batch(self, in_dir, out_dir, params, use_bc7_diffuse, use_bc7_reflection, enabled_maps, worker_count):
        self.log("--- Starting FO76 Spec-Compliant Batch Conversion ---")
        self.log(f"[Info] Generating: {', '.join(sorted(enabled_maps)) if enabled_maps else 'nothing selected'}")

        try:
            texconv_path = find_texconv()
            self.log(f"[Info] Using texconv: {texconv_path}")
        except FileNotFoundError as e:
            self.log(f"[Error] {e}")
            return

        diffuse_files = list(in_dir.rglob("*_d.dds"))

        if not diffuse_files:
            self.log("[Warning] No '*_d.dds' texture sets found.")
            return

        # Phase 1: prepare every set's images (PIL/numpy - fast, sequential is fine).
        self.log(f"[Info] Preparing {len(diffuse_files)} texture set(s)...")
        save_jobs = []
        prep_failed = 0
        for diff_file in diffuse_files:
            norm_file, spec_file = find_set_files(diff_file)
            set_key = get_set_key(diff_file, in_dir)

            rel_dir = diff_file.parent.relative_to(in_dir)
            target_dir = out_dir / rel_dir

            edits_for_set = self.texture_edits.get(set_key, {})

            try:
                jobs = prepare_texture_set_save_jobs(
                    diff_file, norm_file, spec_file, target_dir,
                    use_bc7_diffuse=use_bc7_diffuse,
                    use_bc7_reflection=use_bc7_reflection,
                    texture_edits=edits_for_set,
                    enabled_maps=enabled_maps,
                    **params
                )
                if not jobs:
                    self.log(f"  - Skipped {diff_file.stem}: no output types selected")
                save_jobs.extend(jobs)
            except Exception as e:
                prep_failed += 1
                self.log(f"[Error] Failed to prepare {diff_file.name}: {e}")

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
        self.log(f"[Info] Encoding {len(save_jobs)} file(s) across {len(diffuse_files)} set(s) "
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
        self.log(summary + ".\n")


if __name__ == "__main__":
    root = tk.Tk()
    app = FO76PBRStudioGUI(root)
    root.mainloop()