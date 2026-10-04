#!/usr/bin/env python3
"""
tools/bake_image.py
Bakes high-resolution images/wallpapers into hardware-native 16-bit RGB565 Little-Endian
binary format for ESP32 Rust UI System with center cropping and high-quality Lanczos resampling.

Parameters are configured directly in the code below (not through command-line flags).
"""

import os
import sys

try:
    from PIL import Image
except ImportError:
    print("[!] Error: 'Pillow' is required to bake images.")
    print("    Install it via: pip install Pillow")
    sys.exit(1)

# =============================================================================
# Parameters / Configuration (参数直接配置在代码中，无需命令行参数)
# =============================================================================
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 1. 输入图片路径 (原始高清原图)
INPUT_IMAGE_PATH = os.path.join(WORKSPACE_ROOT, "assets", "wallpapers", "sierra.jpg")

# 2. 输出文件夹路径
OUTPUT_DIR = os.path.join(WORKSPACE_ROOT, "assets", "wallpapers")

# 3. 输出文件名前缀
OUTPUT_NAME = "sierra"

# 4. 输出目标大小 (宽 x 高，壁纸区域为 480 x 430)
OUTPUT_WIDTH = 480
OUTPUT_HEIGHT = 430

# 5. 居中裁剪模式:
#    - 'square': 裁剪原图中心最大正方形 (完整保留雪山主体)，再按目标比例缩放
#    - 'aspect': 保持目标宽高比 (OUTPUT_WIDTH : OUTPUT_HEIGHT) 进行等比例居中裁剪
CROP_MODE = "square"

# =============================================================================
# Implementation
# =============================================================================

def center_crop_and_resize(img, target_w, target_h, mode="square"):
    """
    居中裁剪算法：
    1. 居中裁切：根据 mode 提取原图中心关键区域；
    2. 高保真重采样：使用 Lanczos 滤波器缩放到目标 target_w x target_h 分辨率。
    """
    orig_w, orig_h = img.size
    print(f"[*] Original image resolution: {orig_w}x{orig_h}")

    if mode == "square":
        # 裁剪中心最大正方形 (min(orig_w, orig_h))
        crop_size = min(orig_w, orig_h)
        left = (orig_w - crop_size) // 2
        top = (orig_h - crop_size) // 2
        right = left + crop_size
        bottom = top + crop_size
        print(f"[*] Center square crop: ({left}, {top}, {right}, {bottom}) [{crop_size}x{crop_size}]")
    else:
        # 按目标比例等比居中裁剪
        target_aspect = target_w / target_h
        orig_aspect = orig_w / orig_h
        if orig_aspect > target_aspect:
            crop_h = orig_h
            crop_w = int(orig_h * target_aspect)
        else:
            crop_w = orig_w
            crop_h = int(orig_w / target_aspect)
        left = (orig_w - crop_w) // 2
        top = (orig_h - crop_h) // 2
        right = left + crop_w
        bottom = top + crop_h
        print(f"[*] Center aspect crop ({target_aspect:.3f}): ({left}, {top}, {right}, {bottom}) [{crop_w}x{crop_h}]")

    cropped = img.crop((left, top, right, bottom))
    print(f"[*] Scaling to {target_w}x{target_h} via Lanczos resampling...")
    resized = cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)
    return resized

def bake_rgb565_le(img):
    """
    将 RGB 图片烘焙为 Little-Endian RGB565 二进制字节流。
    每个像素 2 字节:
      低字节: ((G & 0x07) << 5) | (B >> 3)
      高字节: (R & 0xF8) | (G >> 5)
    """
    pixels = img.load()
    w, h = img.size
    raw_bytes = bytearray(w * h * 2)
    idx = 0

    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y][:3]
            r5 = (r >> 3) & 0x1F
            g6 = (g >> 2) & 0x3F
            b5 = (b >> 3) & 0x1F
            val = (r5 << 11) | (g6 << 5) | b5
            raw_bytes[idx] = val & 0xFF
            raw_bytes[idx + 1] = (val >> 8) & 0xFF
            idx += 2

    return bytes(raw_bytes)

def main():
    if not os.path.exists(INPUT_IMAGE_PATH):
        print(f"[!] Error: Input image does not exist: {INPUT_IMAGE_PATH}")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_bin_path = os.path.join(OUTPUT_DIR, f"{OUTPUT_NAME}_{OUTPUT_WIDTH}x{OUTPUT_HEIGHT}.bin")
    out_jpg_path = os.path.join(OUTPUT_DIR, f"{OUTPUT_NAME}_{OUTPUT_WIDTH}x{OUTPUT_HEIGHT}.jpg")

    print(f"[*] Processing: {INPUT_IMAGE_PATH}")
    img = Image.open(INPUT_IMAGE_PATH).convert("RGB")

    # 1. 居中裁剪并缩放
    processed_img = center_crop_and_resize(img, OUTPUT_WIDTH, OUTPUT_HEIGHT, mode=CROP_MODE)

    # 2. 保存预览图
    processed_img.save(out_jpg_path, quality=95)
    print(f"[+] Saved preview image: {out_jpg_path} ({OUTPUT_WIDTH}x{OUTPUT_HEIGHT})")

    # 3. 烘焙为 RGB565 Little-Endian 二进制
    bin_data = bake_rgb565_le(processed_img)
    with open(out_bin_path, "wb") as f:
        f.write(bin_data)
    print(f"[+] Baked RGB565 raw binary: {out_bin_path} ({len(bin_data)} bytes)")

    print("[*] Image baking completed successfully!")

if __name__ == "__main__":
    main()
