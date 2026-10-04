# 问题排查与方案规划: 屏幕局部绘制区域左侧出现黑色噪点竖线

- **所属模块**: `iced-pomelo-gfx`, `iced-pomelo-winit`, `pomelo-gfx`, `board_hal` (`board_display.c`)
- **目标平台**: ESP32-S3 + 480×480 RGB565 CO5300 QSPI AMOLED 面板
- **创建日期**: 2026-10-04
- **状态**: 深入排查中 (Under Investigation)
- **文档编号**: `261004-02`

---

## 一、 问题描述与重现特征

在设备运行过程中，当屏幕发生**局部区域重绘（Damage Redraw）**时，刷新区域的**左侧边缘**偶尔会出现垂直的、由黑色噪点组成的竖线：

1. **场景 1（App Launcher 分页滑动）**：
   - 当在主界面左右滑动切换应用分页（`pomelo_widgets::pager`）时，伴随着卡片/图标的水平位移，某些移动中应用的左侧边缘会竖直出现一条一条断续由黑色杂点/噪点构成的细线。
2. **场景 2（Hello 手写体动画）**：
   - 在 Hello 应用播放动态笔迹生长动画时，处于实时生长的笔尖（tip）局部区域左端，也会偶尔出现类似的黑色噪点竖线。
3. **关键共同点**：
   - 只要是**全屏刷新（Full Screen Repaint）**，界面极其干净，绝对不会出现该黑线；
   - 仅在**局部脏矩形（Partial Damage Rectangle）增量重绘**时触发；
   - 黑线位置恒定出现在**重绘区域的最左边界（$x_1$ 处）**；
   - 视觉呈现为**“黑色噪点构成的竖线”**，而非死黑的纯色色块。

---

## 二、 关键渲染与传输流水线回顾

从 UI 树改变到 AMOLED 显示屏更新，数据流经以下环节：

```text
[ iced 状态变化 ]
        │
        ▼
[ Layer::damage / damage::diff ]  --> 计算新旧两帧的脏矩形集合
        │
        ▼
[ surface.rs: present() ]
   ├─ 1. damage::group & tighten (合并相近矩形)
   ├─ 2. align_hardware_damage (尝试向外扩展到偶数像素)
   ├─ 3. canvas.save() -> canvas.clip_rect(rect)
   ├─ 4. canvas.clear(background) (向 PSRAM 写入背景底色)
   └─ 5. renderer.draw(&mut canvas, rect) (重新绘制变动的组件与文字)
        │
        ▼
[ platform.rs: flush_damage() ]
   └─ 将脏矩形坐标传给底层硬件接口 hal_display_draw_bitmap(x1, y1, x2, y2, pixels, stride)
        │
        ▼
[ board_display.c: hal_display_draw_bitmap() ]
   ├─ 1. x1 = (x1 >> 1) << 1; x2 = ((x2 + 1) >> 1) << 1; (CO5300 强制偶数对齐)
   ├─ 2. src = pixels + y1 * stride + x1; (PSRAM 帧缓冲区切片)
   ├─ 3. copy_row_rgb565_be(in, out, width); (CPU 搬运到 internal SRAM 并做大小端变换)
   └─ 4. esp_lcd_panel_draw_bitmap(..., s_dma_chunk); (QSPI DMA 发往 CO5300)
```

---

## 三、 当前工作区的临时修改与遗留状态

在当前会话的探索中，工作区暂存了以下代码改动（尚未彻底解决问题）：

1. **`iced-pomelo-gfx/src/surface.rs`**:
   - 增加了 `align_hardware_damage` 函数，将 Damage 矩形通过 `(floor() / 2 * 2)` 和 `((ceil() + 1) / 2 * 2)` 强行向外扩大到偶数边界。
2. **`iced-pomelo-winit/src/platform.rs`**:
   - 在 `flush_damage` 中同样加入了类似的向外偶数对齐计算。
3. **`pomelo-gfx/src/raster.rs`**:
   - 将 `fill_rect`、`fill_circle`、`stroke_polyline`、`fill_convex_quad` 等光栅化函数的包围盒由 `.round()` 改为了 `.floor()` 与 `.ceil()`。
4. **`board_hal/board_display.c`**:
   - 已将 CO5300 AMOLED 的 QSPI 像素时钟（`io_config.pclk_hz`）由 80MHz 改回 40MHz（`40 * 1000 * 1000`），以排除高频信号完整性与高频总线毛刺问题。

**当前现象表明**：即便做了上述改动，黑线依然偶发，说明根因不在于单纯的某一处浮点数向最近邻舍入，而在于**软件重绘/清屏边界与硬件传输切片边界之间的“离散不同步”或“Diff 算法对原位置边缘的漏算”**。

---

## 四、 核心怀疑点与深入分析

### 怀疑点 1：软件清屏（`canvas.clear`）与硬件搬运（`src` 指针）存在 1 像素切片错位（最可能）

#### 机制推导：
- CO5300 的硬件特性要求其 QSPI CASET（列地址设置）必须是偶数对齐。
- 在 `board_display.c` 中：
  ```c
  x1 = (x1 >> 1) << 1;
  const uint16_t *src = pixels + (size_t)y1 * (size_t)stride + (size_t)x1;
  ```
  如果传入底层的 `x1` 是奇数（例如 73），底层会将其强行修正为 72，并从 `pixels + 72` 开始读取显存。
- 如果上层在软件绘制时：
  - 上层的 `canvas.clear` 或组件绘制只覆盖了 $[73, x_2]$；
  - 那么 `pixels + 72` 的这一列像素在本次刷新中**完全没有被清空，也没有被重绘**！
- **为什么会呈现“由黑色噪点组成的竖线”？**
  1. `Pixmap565` 底层是在 PSRAM 中开辟的大块内存，初始清零（`0x0000` = 纯黑）。如果某一列从未被该区域写入过，或者残留了之前旧帧的边缘 Alpha 混合值；
  2. 加上抗锯齿（Coverage）、抖动渐变（Dither）在边缘采样时计算了渐变过渡，这一列未清空的旧底色就会透过半透明混合或者边界截断暴露出来，形成一整列断续的黑色噪点！

#### 二次剪裁嵌套问题：
在 `iced-pomelo-gfx/src/geometry.rs` 的 `draw` 以及 `text.rs` 中：
```rust
let Some(clip) = clip_bounds
    .intersection(&Rectangle {
        x: damage.left(),
        y: damage.top(),
        width: damage.width,
        height: damage.height,
    })
    .and_then(rect)
else { return; };

canvas.save();
canvas.clip_rect(clip);
```
即便最外层 `surface.rs` 对 `canvas` 做了偶数清屏，内部的组件绘制时又用 `item.clip_bounds()` 与 `damage` 做了一次求交，如果内部的 `clip_bounds` 是浮点数未对齐的（比如滑动过程中的 $x = 24.7$），那么组件绘制的最左侧像素是 $25$，而清屏清到了 $24$，导致背景底色与组件之间出现未重绘的缝隙。

---

### 怀疑点 2：滑动时 `Layer::damage` / `diff` 漏算了“旧位置”左侧的抗锯齿延展

#### 机制推导：
- 当一个图标或卡片向右滑动（例如从 $X_{old} = 10.0$ 移动到 $X_{new} = 15.0$）时：
  - 旧位置 $X_{old}$ 的最左边缘需要被重绘为背景色（即所谓的“擦除残影”）。
- 图标采用圆角矩形（`rrect`），在边缘包含 1 像素的抗锯齿羽化带（Coverage $< 1.0$）：
  ```rust
  let r_outer = rx + 0.5;
  ```
- 如果上一帧记录的 `Primitive::bounds()` 仅为几何矩形 $[10, 10 + W]$，而实际抗锯齿光栅化向外渲染到了 $9.5$ 像素；
- 那么在计算 damage 时，如果只擦除到 $[10, \dots]$，在 $x = 9$ 处残留的半透明暗色抗锯齿边缘就永远无法被擦除，从而随着滑动留下一列暗色噪点。

---

### 怀疑点 3：`copy_row_rgb565_be` 32 位 SIMD 优化边界

在 `board_display.c` 中：
```c
if (((uintptr_t)src & 3u) == 0 && ((uintptr_t)dst & 3u) == 0) {
    const uint32_t *src32 = (const uint32_t *)src;
    uint32_t *dst32 = (uint32_t *)dst;
    int32_t words = width / 2;
    for (int32_t i = 0; i < words; ++i) {
        uint32_t val = src32[i];
        dst32[i] = ((val & 0x00FF00FF) << 8) | ((val & 0xFF00FF00) >> 8);
    }
    if (width & 1) {
        dst[width - 1] = __builtin_bswap16(src[width - 1]);
    }
}
```
需要核实当 `width` 为奇数，或 `src` 处于 PSRAM 缓存行（Cache Line）边缘时，是否出现读越界或首字字节序颠倒。

---

## 五、 下次继续时的行动清单 (Next Steps)

1. **统一硬件对齐的单一可信源 (Single Source of Truth for Alignment)**：
   - 审查整个链路：不要在 `surface.rs`、`platform.rs` 和 `board_display.c` 三处各自做 `(x >> 1) << 1`；
   - 应当在 `iced-pomelo-gfx` 生成 Damage 矩形的第一时间，就将其**规整化为严格满足硬件约束的整数物理像素矩形**（$x_1$ 偶数、$y_1$ 偶数、$x_2$ 偶数、$y_2$ 偶数）；
   - 保证后续所有阶段（`canvas.clip_rect`、`canvas.clear`、`flush_damage`、`src` 计算）使用的都是这组完全相同的坐标。

2. **验证抗锯齿向外延展 1 像素 (Anti-aliasing Safety Margin)**：
   - 检查 `Primitive::bounds()`（特别是圆角矩形 `Rounded` 和矢量曲线 `Stroke`），确认其包围盒在向外膨胀时是否完整囊括了 `r_outer = r + 0.5` 的抗锯齿衰减范围。

3. **真实滑动坐标采样与日志排查**：
   - 在 `flush_damage` 和 `board_display.c` 中增加针对不规则坐标的断言日志，排查在复现黑色竖线瞬间，传入的 $(x_1, y_1, x_2, y_2)$ 是否存在奇数或未匹配情况。
