#!/usr/bin/env python3
# Version: 5.0

import os
import sys
import math
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, colorchooser

# ------------------------------------------------------------------------------
# Check for Dependencies
# ------------------------------------------------------------------------------
def ensure_dependencies():
    missing = []
    try:
        from PIL import Image, ImageStat, ImageFilter, ImageOps, ImageTk, ImageChops
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
from PIL import Image, ImageStat, ImageFilter, ImageOps, ImageTk, ImageChops


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
# Material Presets Library: (Smoothness, Metalness, LightMult, SpecBoost, DefaultColor)
# Need to define this so we can have some base materials to go off of
# ------------------------------------------------------------------------------
MATERIAL_PRESETS = {
    "Chrome":           (250, 255, 1.0, 2.0, (220, 220, 225)),
    "Polished Steel":   (210, 240, 1.0, 1.3, (180, 180, 185)),
    "Gold / Brass":     (230, 250, 1.0, 1.5, (230, 180, 70)),
    "Copper":           (210, 245, 1.0, 1.4, (215, 115, 80)),
    "Rusted / Dull":    (110, 180, 0.9, 0.6, (140, 75, 50)),
    "Painted Metal":    (150, 80,  1.0, 0.8, (60, 120, 190)),
    "Leather":          (120, 15,  1.0, 0.5, (110, 65, 40)),
    "Cloth / Fabric":   (40,  0,   1.0, 0.2, (150, 150, 150)),
    "Dull Unpolished":  (80,  120, 0.9, 0.5, (100, 100, 105))
}

# This is used for the sphere, we average it out to get the base color
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
# 3D Sphere Raytracer & Shader Simulation
# Added this to preview the content since python doesn't natively have a 3d viewer for meshes (as far as I know at least)
# 
# ------------------------------------------------------------------------------
class PBRSphereRenderer:
    def __init__(self, width=220, height=220):
        self.width = width
        self.height = height
        self.center_x = width // 2
        self.center_y = height // 2
        self.radius = min(width, height) // 2 - 12
        
        lx, ly, lz = 0.5, 0.7, 0.8
        l_len = math.sqrt(lx*lx + ly*ly + lz*lz)
        self.light_dir = (lx/l_len, ly/l_len, lz/l_len)

    def render(self, smoothness: float, metalness: float, base_color: tuple, light_mult: float, spec_boost: float) -> Image.Image:
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
                dist_sq = dx*dx + dy*dy

                if dist_sq <= self.radius * self.radius:
                    nz = math.sqrt(max(0.0, self.radius*self.radius - dist_sq))
                    nx = dx / self.radius
                    ny = dy / self.radius
                    nz = nz / self.radius

                    ndotl = max(0.0, nx*lx + ny*ly + nz*lz)

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
def process_fo76_normal_map(norm_path: Path) -> "Image.Image":

    img_n = Image.open(norm_path).convert("RGBA")

    # DirectX Normal Conversion
    r_chan = img_n.getchannel("R")
    g_chan = Image.eval(img_n.getchannel("G"), lambda x: 255 - x)  # Flip Y axis for FO76
    b_placeholder = Image.new("L", img_n.size, 128)

    return Image.merge("RGB", (r_chan, g_chan, b_placeholder))


# Building the reflection map
# Requires us to build it via the diffuse map and the specular map
# Need to make sure they're all the same size and correct if not
# Also need to take the adjustments into account
def process_fo76_reflection_map(
    diffuse_img: Image.Image,
    spec_path: Path,
    target_size,
    reflection_strength: float = 1.6,
    contrast: float = 1.35,
    bias: float = 0.0
) -> "Image.Image":

    if diffuse_img.size != target_size:
        diffuse_img = diffuse_img.resize(target_size, Image.Resampling.LANCZOS)
    diffuse_rgb = diffuse_img.convert("RGB")
    diffuse_arr = np.asarray(diffuse_rgb, dtype=np.float32) / 255.0
    diffuse_luma = (0.299 * diffuse_arr[:, :, 0] +
                    0.587 * diffuse_arr[:, :, 1] +
                    0.114 * diffuse_arr[:, :, 2])

    if spec_path and spec_path.exists():
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
def bake_ao_from_normal_and_diffuse(
    normal_path: Path,
    diffuse_img: "Image.Image",
    target_size,
    ao_strength: float = 1.4,
    blur_radius: float = 3.0,
    diffuse_weight: float = 0.35,
) -> "Image.Image":

    # Normal-based cavity term
    if normal_path and Path(normal_path).exists():
        img_n = Image.open(normal_path).convert("RGB")
        if img_n.size != target_size:
            img_n = img_n.resize(target_size, Image.Resampling.LANCZOS)
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

    # If we want a white AO map via the checkbox, we creaate it here
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


# ------------------------------------------------------------------------------
# PBR Image Conversion Pipeline
# ------------------------------------------------------------------------------
def process_texture_set_pbr(diffuse_path: Path, normal_path: Path, spec_path: Path,
                            output_dir: Path,
                            smooth_offset: int, metal_offset: int, light_mult: float,
                            reflection_boost: float = 1.6,
                            use_white_ao: bool = True,
                            ao_strength: float = 1.4,
                            sss_strength: int = 0,
                            use_bc7_diffuse: bool = False,
                            use_bc7_reflection: bool = False,
                            log_func=print):
    base_name = diffuse_path.stem.rsplit('_', 1)[0]
    output_dir.mkdir(parents=True, exist_ok=True)

    img_d = Image.open(diffuse_path) if diffuse_path and diffuse_path.exists() else None
    target_size = img_d.size if img_d else (2048, 2048)
    original_diffuse_img = img_d.copy() if img_d else None

    if img_d:
        has_alpha = img_d.mode in ("RGBA", "LA") or (img_d.mode == "P" and "transparency" in img_d.info)

        if light_mult != 1.0:
            img_d_rgb = img_d.convert("RGB")
            img_d_rgb = Image.eval(img_d_rgb, lambda x: int(min(255, x * light_mult)))
            if has_alpha:
                r, g, b = img_d_rgb.split()
                a = img_d.convert("RGBA").getchannel("A")
                img_d = Image.merge("RGBA", (r, g, b, a))
            else:
                img_d = img_d_rgb

        if use_bc7_diffuse:
            bc_fmt = "BC7_UNORM_SRGB"
        else:
            bc_fmt = "BC3_UNORM_SRGB" if has_alpha else "BC1_UNORM_SRGB"
        save_dds(img_d, output_dir / f"{base_name}_d.dds", bc_fmt, log_func=log_func)


    if normal_path and normal_path.exists():
        img_n = process_fo76_normal_map(normal_path)
        save_dds(img_n, output_dir / f"{base_name}_n.dds", "BC5_SNORM",
                 is_normal_map=True, log_func=log_func)


    if original_diffuse_img:
        reflectivity_bias = metal_offset / 255.0
        img_r = process_fo76_reflection_map(
            original_diffuse_img,
            spec_path,
            target_size,
            reflection_strength=reflection_boost,
            bias=reflectivity_bias
        )
        r_fmt = "BC7_UNORM_SRGB" if use_bc7_reflection else "BC1_UNORM_SRGB"
        save_dds(img_r, output_dir / f"{base_name}_r.dds", r_fmt, log_func=log_func)

    img_l = process_fo76_lightmap(
        normal_path, spec_path, original_diffuse_img, target_size,
        use_white_ao=use_white_ao, ao_strength=ao_strength, sss_strength=sss_strength
    )
    save_dds(img_l, output_dir / f"{base_name}_l.dds", "BC1_UNORM", log_func=log_func)

    d_fmt_label = "BC7 sRGB" if use_bc7_diffuse else "BC1/BC3 sRGB"
    r_fmt_label = "BC7 sRGB" if use_bc7_reflection else "BC1 sRGB"
    log_func(f"  ✓ Saved FO76 set: {base_name} (_d [{d_fmt_label}], _n [BC5_SNORM], "
              f"_r [{r_fmt_label}], _l [BC1, Spec.G/AO(normal+diffuse)/SSS])")


# ------------------------------------------------------------------------------
# Main Application GUI
# ------------------------------------------------------------------------------
class FO76PBRStudioGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("FO4 → FO76 PBR Converter & Real-Time Material Studio")
        self.root.geometry("980x760")
        self.root.minsize(880, 650)

        self.sphere_renderer = PBRSphereRenderer(width=220, height=220)
        self.preview_image_tk = None
        self.current_base_color = (180, 180, 185)
        self.last_checked_input_path = ""

        self.create_widgets()
        self.update_sphere_preview()

    def create_widgets(self):
        header = ttk.Frame(self.root, padding="10")
        header.pack(fill=tk.X)
        ttk.Label(header, text="FO76 Material & PBR Calibration Studio", font=("Helvetica", 14, "bold")).pack(anchor=tk.W)
        ttk.Label(header, text="Adjust live metalness, gloss, specular, and base color response before batch processing.").pack(anchor=tk.W)

        main_container = ttk.Frame(self.root, padding="5")
        main_container.pack(fill=tk.BOTH, expand=True)

        left_panel = ttk.Frame(main_container)
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        right_panel = ttk.LabelFrame(main_container, text=" Real-Time PBR Viewport ", padding="10")
        right_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=5, pady=5)

        # 1. PATH SELECTION
        io_frame = ttk.LabelFrame(left_panel, text=" Paths ", padding="8")
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
        slider_frame = ttk.LabelFrame(left_panel, text=" Live PBR Material Controls ", padding="10")
        slider_frame.pack(fill=tk.X, pady=4)

        ttk.Label(slider_frame, text="Material Preset:").grid(row=0, column=0, sticky=tk.W)
        self.preset_combo = ttk.Combobox(slider_frame, values=list(MATERIAL_PRESETS.keys()))
        self.preset_combo.grid(row=0, column=1, sticky=tk.EW, padx=5, pady=4)
        self.preset_combo.set("Polished Steel")
        self.preset_combo.bind("<<ComboboxSelected>>", self.on_preset_selected)
        self.preset_combo.bind("<KeyRelease>", self.filter_presets)

        ttk.Label(slider_frame, text="Smoothness / Gloss Preview:").grid(row=1, column=0, sticky=tk.W)
        self.smooth_slider = ttk.Scale(slider_frame, from_=0, to=255, value=210, command=lambda v: self.on_slider_change())
        self.smooth_slider.grid(row=1, column=1, sticky=tk.EW, padx=5, pady=4)
        self.smooth_val_lbl = ttk.Label(slider_frame, text="210", width=5)
        self.smooth_val_lbl.grid(row=1, column=2)

        ttk.Label(slider_frame, text="Reflectivity Bias (_r.dds):").grid(row=2, column=0, sticky=tk.W)
        self.metal_slider = ttk.Scale(slider_frame, from_=0, to=255, value=240, command=lambda v: self.on_slider_change())
        self.metal_slider.grid(row=2, column=1, sticky=tk.EW, padx=5, pady=4)
        self.metal_val_lbl = ttk.Label(slider_frame, text="240", width=5)
        self.metal_val_lbl.grid(row=2, column=2)

        ttk.Label(slider_frame, text="Albedo Light Multiplier:").grid(row=3, column=0, sticky=tk.W)
        self.light_slider = ttk.Scale(slider_frame, from_=0.2, to=2.0, value=1.0, command=lambda v: self.on_slider_change())
        self.light_slider.grid(row=3, column=1, sticky=tk.EW, padx=5, pady=4)
        self.light_val_lbl = ttk.Label(slider_frame, text="1.00x", width=5)
        self.light_val_lbl.grid(row=3, column=2)

        ttk.Label(slider_frame, text="Reflection Map Strength (_r.dds):").grid(row=4, column=0, sticky=tk.W)
        self.spec_slider = ttk.Scale(slider_frame, from_=0.1, to=3.0, value=1.6, command=lambda v: self.on_slider_change())
        self.spec_slider.grid(row=4, column=1, sticky=tk.EW, padx=5, pady=4)
        self.spec_val_lbl = ttk.Label(slider_frame, text="1.60x", width=5)
        self.spec_val_lbl.grid(row=4, column=2)

        self.white_ao_var = tk.BooleanVar(value=False)
        self.white_ao_chk = ttk.Checkbutton(slider_frame, text="Use Pure White Lightmap AO (_l Green = 255)", variable=self.white_ao_var)
        self.white_ao_chk.grid(row=5, column=0, columnspan=3, sticky=tk.W, pady=4)

        self.bc7_diffuse_var = tk.BooleanVar(value=False)
        self.bc7_diffuse_chk = ttk.Checkbutton(
            slider_frame,
            text="Use BC7 (sRGB - DX11) for Diffuse (_d.dds) [Higher Quality, Larger File]",
            variable=self.bc7_diffuse_var
        )
        self.bc7_diffuse_chk.grid(row=7, column=0, columnspan=3, sticky=tk.W, pady=4)

        self.bc7_reflection_var = tk.BooleanVar(value=False)
        self.bc7_reflection_chk = ttk.Checkbutton(
            slider_frame,
            text="Use BC7 (sRGB - DX11) for Reflection (_r.dds) [Higher Quality, Larger File]",
            variable=self.bc7_reflection_var
        )
        self.bc7_reflection_chk.grid(row=8, column=0, columnspan=3, sticky=tk.W, pady=4)

        ttk.Label(slider_frame, text="Subsurface Scattering (_l.dds Blue):").grid(row=6, column=0, sticky=tk.W)
        self.sss_slider = ttk.Scale(slider_frame, from_=0, to=255, value=0, command=lambda v: self.on_slider_change())
        self.sss_slider.grid(row=6, column=1, sticky=tk.EW, padx=5, pady=4)
        self.sss_val_lbl = ttk.Label(slider_frame, text="0", width=5)
        self.sss_val_lbl.grid(row=6, column=2)

        slider_frame.columnconfigure(1, weight=1)

        # 3. CONSOLE LOG
        log_frame = ttk.LabelFrame(left_panel, text=" Console Log ", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=4)

        self.log_widget = scrolledtext.ScrolledText(log_frame, state='disabled', wrap=tk.WORD, height=8)
        self.log_widget.pack(fill=tk.BOTH, expand=True)

        # 4. VIEWPORT PANEL
        self.canvas_preview = tk.Canvas(right_panel, width=220, height=220, bg="#18191c", highlightthickness=0)
        self.canvas_preview.pack(padx=5, pady=5)

        color_frame = ttk.Frame(right_panel)
        color_frame.pack(fill=tk.X, pady=4)
        ttk.Label(color_frame, text="Base Color:").pack(side=tk.LEFT, padx=2)
        
        self.color_swatch = tk.Canvas(color_frame, width=24, height=18, bg="#b4b4b9", relief=tk.SUNKEN, bd=1)
        self.color_swatch.pack(side=tk.LEFT, padx=4)
        
        ttk.Button(color_frame, text="Pick Color", command=self.pick_custom_color).pack(side=tk.LEFT, padx=2)

        ttk.Label(right_panel, text="Preview Mode:", font=("Helvetica", 9, "bold")).pack(anchor=tk.W, pady=(10, 2))
        self.view_mode = tk.StringVar(value="pbr")
        ttk.Radiobutton(right_panel, text="3D Lit Sphere (Combined)", variable=self.view_mode, value="pbr", command=self.update_sphere_preview).pack(anchor=tk.W)

        btn_run = ttk.Button(right_panel, text="▶ Convert Textures", command=self.start_conversion)
        btn_run.pack(fill=tk.X, pady=(20, 5))

    def check_and_update_input_path(self):
        raw_path = self.input_entry.get().strip().strip('"')
        if raw_path and raw_path != self.last_checked_input_path:
            p = Path(raw_path)
            if p.exists() and p.is_dir():
                self.last_checked_input_path = raw_path
                diff_files = list(p.rglob("*_d.dds"))
                if diff_files:
                    avg_col = get_average_color(diff_files[0])
                    self.set_base_color(avg_col)
                    self.log(f"[Info] Auto-detected texture color: {diff_files[0].name} -> RGB{avg_col}")

    def on_slider_change(self):
        smooth = int(self.smooth_slider.get())
        metal = int(self.metal_slider.get())
        light = float(self.light_slider.get())
        spec = float(self.spec_slider.get())
        sss = int(self.sss_slider.get())

        self.smooth_val_lbl.config(text=f"{smooth}")
        self.metal_val_lbl.config(text=f"{metal}")
        self.light_val_lbl.config(text=f"{light:.2f}x")
        self.spec_val_lbl.config(text=f"{spec:.2f}x")
        self.sss_val_lbl.config(text=f"{sss}")

        self.update_sphere_preview()

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
            smooth, metal, light, spec, color = MATERIAL_PRESETS[preset_name]
            self.smooth_slider.set(smooth)
            self.metal_slider.set(metal)
            self.light_slider.set(light)
            self.spec_slider.set(spec)
            
            if self.last_checked_input_path == "":
                self.set_base_color(color)
            else:
                self.on_slider_change()

    def set_base_color(self, rgb_tuple):
        self.current_base_color = rgb_tuple
        hex_col = f"#{rgb_tuple[0]:02x}{rgb_tuple[1]:02x}{rgb_tuple[2]:02x}"
        self.color_swatch.config(bg=hex_col)
        self.update_sphere_preview()

    def pick_custom_color(self):
        color_code = colorchooser.askcolor(title="Choose Material Base Color")
        if color_code and color_code[0]:
            r, g, b = [int(c) for c in color_code[0]]
            self.set_base_color((r, g, b))

    def update_sphere_preview(self):
        smooth = self.smooth_slider.get()
        metal = self.metal_slider.get()
        light = self.light_slider.get()
        spec = self.spec_slider.get()

        img = self.sphere_renderer.render(
            smoothness=smooth,
            metalness=metal,
            base_color=self.current_base_color,
            light_mult=light,
            spec_boost=spec
        )

        self.preview_image_tk = ImageTk.PhotoImage(img)
        self.canvas_preview.create_image(0, 0, image=self.preview_image_tk, anchor=tk.NW)

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
        self.log_widget.config(state='normal')
        self.log_widget.insert(tk.END, message + "\n")
        self.log_widget.see(tk.END)
        self.log_widget.config(state='disabled')

    def start_conversion(self):
        in_dir = self.input_entry.get().strip().strip('"')
        out_dir = self.output_entry.get().strip().strip('"')

        if not in_dir or not Path(in_dir).exists():
            messagebox.showerror("Error", "Please select a valid input directory.")
            return
        if not out_dir:
            messagebox.showerror("Error", "Please select an output directory.")
            return

        smooth_off = int(self.smooth_slider.get()) - 180
        metal_off = int(self.metal_slider.get()) - 180
        light_mult = float(self.light_slider.get())
        reflection_boost = float(self.spec_slider.get())
        use_white_ao = self.white_ao_var.get()
        sss_strength = int(self.sss_slider.get())
        use_bc7_diffuse = self.bc7_diffuse_var.get()
        use_bc7_reflection = self.bc7_reflection_var.get()

        threading.Thread(
            target=self.run_batch,
            args=(Path(in_dir), Path(out_dir), smooth_off, metal_off, light_mult,
                  reflection_boost, use_white_ao, sss_strength, use_bc7_diffuse, use_bc7_reflection),
            daemon=True
        ).start()

    def run_batch(self, in_dir, out_dir, smooth_off, metal_off, light_mult,
                  reflection_boost, use_white_ao, sss_strength, use_bc7_diffuse, use_bc7_reflection):
        self.log("--- Starting FO76 Spec-Compliant Batch Conversion ---")

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

        succeeded, failed = 0, 0
        for diff_file in diffuse_files:
            base_prefix = str(diff_file)[:-6]
            norm_file = Path(f"{base_prefix}_n.dds")
            spec_file = Path(f"{base_prefix}_s.dds")
            if not spec_file.exists():
                spec_file = Path(f"{base_prefix}_r.dds")

            rel_dir = diff_file.parent.relative_to(in_dir)
            target_dir = out_dir / rel_dir

            try:
                process_texture_set_pbr(
                    diff_file, norm_file, spec_file, target_dir,
                    smooth_offset=smooth_off, metal_offset=metal_off, light_mult=light_mult,
                    reflection_boost=reflection_boost,
                    use_white_ao=use_white_ao, sss_strength=sss_strength,
                    use_bc7_diffuse=use_bc7_diffuse,
                    use_bc7_reflection=use_bc7_reflection,
                    log_func=self.log
                )
                succeeded += 1
            except Exception as e:
                failed += 1
                self.log(f"[Error] Failed to convert {diff_file.name}: {e}")

        self.log(f"\n✔ Batch Conversion Finished! {succeeded} set(s) exported, {failed} failed.\n")


if __name__ == "__main__":
    root = tk.Tk()
    app = FO76PBRStudioGUI(root)
    root.mainloop()