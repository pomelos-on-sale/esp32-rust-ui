#!/usr/bin/env python3
"""
tools/bake_app_icons.py
Bakes high-resolution 256x256 application PNG icons into hardware-native 16-bit
RGB565 Little-Endian bitmaps and 8-bit Alpha masks with authentic Apple squircle masking.

Parameters are configured directly in the code below (not through command-line flags).
"""

import os
import sys
import math

try:
    from PIL import Image
except ImportError:
    print("[!] Error: 'Pillow' is required to bake app icons.")
    print("    Install it via: pip install Pillow")
    sys.exit(1)

# =============================================================================
# Parameters / Configuration (参数直接配置在代码中)
# =============================================================================
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 1. 输入文件夹：应用图标高清 PNG 源素材目录
INPUT_DIR = os.path.join(WORKSPACE_ROOT, "assets", "app_icons", "source")

# 2. 输出文件夹：应用图标烘焙硬件二进制输出目录
OUTPUT_DIR = os.path.join(WORKSPACE_ROOT, "assets", "app_icons", "baked")

# 3. 输出图标大小 (宽 = 高 = OUTPUT_SIZE)
OUTPUT_SIZE = 128

# 4. iOS 连续曲率 Squircle 圆角比例 (标准 Apple squircle ratio ~0.225)
SQUIRCLE_RADIUS_RATIO = 0.225

# =============================================================================
# Implementation
# =============================================================================

def rgb888_to_rgb565(r, g, b):
    """Convert 8-bit RGB to 16-bit RGB565 Little-Endian."""
    r5 = (r >> 3) & 0x1F
    g6 = (g >> 2) & 0x3F
    b5 = (b >> 3) & 0x1F
    val = (r5 << 11) | (g6 << 5) | b5
    return val

def generate_squircle_mask(size, radius_ratio=0.225):
    """Generate an authentic iOS squircle alpha mask (size x size)."""
    mask = bytearray(size * size)
    half = size * 0.5
    corner_r = radius_ratio * size
    inner = half - corner_r

    for y in range(size):
        dy = abs(y + 0.5 - half)
        qy = max(0.0, dy - inner)
        row_offset = y * size
        for x in range(size):
            dx = abs(x + 0.5 - half)
            qx = max(0.0, dx - inner)
            dist = math.sqrt(qx * qx + qy * qy)
            if dist <= corner_r - 1.2:
                mask[row_offset + x] = 255
            elif dist < corner_r:
                factor = (corner_r - dist) / 1.2
                mask[row_offset + x] = int(255 * factor)
            else:
                mask[row_offset + x] = 0
    return mask

def bake_app_icons():
    if not os.path.exists(INPUT_DIR):
        print(f"[!] Error: Input directory '{INPUT_DIR}' not found!")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    squircle_mask = generate_squircle_mask(OUTPUT_SIZE, SQUIRCLE_RADIUS_RATIO)
    png_files = sorted([f for f in os.listdir(INPUT_DIR) if f.endswith(".png")])

    print("=== ESP32 Rust UI App Icon Baker ===")
    print(f"Input Directory  : {INPUT_DIR}")
    print(f"Output Directory : {OUTPUT_DIR}")
    print(f"Output Icon Size : {OUTPUT_SIZE}x{OUTPUT_SIZE} (Squircle Ratio: {SQUIRCLE_RADIUS_RATIO})")
    print(f"Found {len(png_files)} PNG icons to bake.\n")

    for filename in png_files:
        name = os.path.splitext(filename)[0]
        src_path = os.path.join(INPUT_DIR, filename)

        img = Image.open(src_path).convert("RGBA")
        if img.size != (OUTPUT_SIZE, OUTPUT_SIZE):
            img = img.resize((OUTPUT_SIZE, OUTPUT_SIZE), Image.Resampling.LANCZOS)

        pixels = img.load()
        rgb565_data = bytearray(OUTPUT_SIZE * OUTPUT_SIZE * 2)
        alpha_data = bytearray(OUTPUT_SIZE * OUTPUT_SIZE)

        idx_rgb = 0
        for y in range(OUTPUT_SIZE):
            row_offset = y * OUTPUT_SIZE
            for x in range(OUTPUT_SIZE):
                r, g, b, a = pixels[x, y]
                sq_a = squircle_mask[row_offset + x]
                final_a = int((a * sq_a) / 255)

                val565 = rgb888_to_rgb565(r, g, b)
                rgb565_data[idx_rgb] = val565 & 0xFF
                rgb565_data[idx_rgb + 1] = (val565 >> 8) & 0xFF
                idx_rgb += 2

                alpha_data[row_offset + x] = final_a

        out_rgb_path = os.path.join(OUTPUT_DIR, f"{name}_rgb565.bin")
        out_alpha_path = os.path.join(OUTPUT_DIR, f"{name}_alpha.bin")

        with open(out_rgb_path, "wb") as f_rgb:
            f_rgb.write(rgb565_data)
        with open(out_alpha_path, "wb") as f_a:
            f_a.write(alpha_data)

        print(f"[+] Baked: {name:15s} -> {len(rgb565_data)} B (RGB565), {len(alpha_data)} B (Alpha)")

    print(f"\n=== Successfully Baked All {len(png_files)} App Icons to {OUTPUT_DIR} ===")

if __name__ == "__main__":
    bake_app_icons()
