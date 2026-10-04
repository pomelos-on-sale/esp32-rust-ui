# 待办规划: 阻塞 IO 与图片渲染通路 (Blocking I/O & Image Pipeline)

- **模块**: `firmware/components/board_hal` / `firmware/pomelo-hal-esp32` / `vendor/pomelo-hal` / `vendor/iced-pomelo-gfx` / `apps/*`
- **硬件环境**: Waveshare ESP32-S3-Touch-AMOLED-2.16 (双核 Xtensa LX7 @ 240MHz, 8MB PSRAM, 16MB Flash, 480x480 AMOLED)
- **状态**: 设计评估完成（2026-09-29），未实现
- **关联**: `260927-01 §4.4`（AppStore 在线下载）、`260929-04`（HTTPS 与系统校时）、`260926-03 §3.1`（「发起 + 轮询状态」约定）、`260929-02`（HAL 不引入 async 的结论）

---

## 一、 结论摘要

> **问**：现在能不能"异步下载一张图片，下载完立即加载并更新 UI"？

**答：框架层面的管道已经通了，但有三个必须知道的事：**

1. ✅ **后台完成 → 消息 → 立刻重绘：成立**，设备上延迟 ≈ 1 ms（见 §二）。
2. ⚠️ **不能用 async 包阻塞调用**：这类调用在单线程执行器上会冻住整个 UI，而**桌面会用 thread-pool 把这个问题掩盖掉**（见 §三）。
3. ❌ **渲染器目前画不出图片**：`allocate_image` / `draw_image` 都是空的。**这才是真正的主要工作量**（见 §四）。

---

## 二、 为什么"下载完立即更新 UI"在架构上成立

设备循环的形状正好适合这件事：

- `firmware/components/rust_main/src/lib.rs:246`：循环每轮 `vTaskDelay(1)`（≈1 ms），**free-running**，每轮都调 `step()` → `frame()`。
- `vendor/iced-pomelo-winit/src/board.rs` 的 `frame()`：**先** `self.messages.extend(self.program.poll())`（推进执行器），**再**判断跳帧；跳帧条件是
  `!dirty && !redraw_requested && tree.is_idle() && messages.is_empty()`。

⇒ **后台任务完事 → 消息进 `messages` → 同一轮就画出来**，而且**不需要** app 处在动画状态。桌面是事件驱动的，反而没有这个性质；这里的 1 kHz 轮询是白送的。

- `Host::update(message)`（`board.rs:236-252`）是官方留的「从 widget 树外面塞消息」的门 —— 硬件按键和固件推的 `Message::Status` 都走这里。
- `Task` / `Pump` 已就绪，见 `260929-02 §二`。

---

## 三、 坑 1：阻塞 ≠ 异步（而且桌面会掩盖它）

| 目标 | 执行器 | 后果 |
| :--- | :--- | :--- |
| **设备** | `Pump` —— **一个由帧循环排空的单线程队列**（`vendor/iced-pomelo-winit/src/executor.rs`） | `Task::perform(async { blocking_http_get() })` 会在**帧循环里**被 poll ⇒ 下载期间整个 UI 冻死 |
| **桌面** | iced 默认的 **thread-pool** 执行器（`apps/*/Cargo.toml` 的 `features = ["thread-pool", …]`） | 同一段代码跑得好好的，**194 个测试全绿** |

⇒ **这是最坏的一类不对称**：桌面验证通过，上设备卡死。

**结论：阻塞 IO 必须离开帧循环。** 即使 future 会 yield，每次 poll 照样阻塞，所以"把它包成 async"不解决问题。

---

## 四、 坑 2：真正的缺口是渲染器没有图像通路

- `vendor/iced-pomelo-gfx/src/renderer.rs:346`：`allocate_image(…)` → `callback(Err(image::Error::Unsupported))`
- `vendor/iced-pomelo-gfx/src/geometry.rs:463`：`draw_image(…)` 是**空实现**

注释写明了这是**故意的**："The launcher has none on purpose — its wallpaper and icons are **baked RGB565 blits** that `pomelo-gfx` draws natively"。

⇒ 即使字节到手，也**没有通路显示**。两条路线：

| | 做法 | 代价 / 风险 |
| :--- | :--- | :--- |
| **A（建议先做）** | 给 `pomelo-gfx` 加**原生 bitmap 图元**（RGB565 + `Arc<[u16]>`），像 baked 壁纸那样直接 blit；app 侧一个小 widget | 不动 `image` crate、不动 `iced_graphics` 的 `image` feature；需要自己处理缩放 / 裁剪 / damage |
| **B（更 iced）** | 开 `iced_graphics` 的 `image` feature + 固件引入 `image` crate，实现 `allocate_image`（解码 → 上传为纹理）与 `draw_image`（带缩放裁剪的 RGB565 blit） | `image` crate 体积大、在 xtensa 上不一定编得过；**且它的解码跑在 renderer 自己的 task 上 = 跑在帧循环里**，又回到 §三 |

**无论走哪条：解码都必须在帧循环之外** —— 480×480 的 JPEG 解码是 100 ms 量级。

---

## 五、 推荐形状：任务 + 非阻塞 poll（本仓库已有的约定）

这不是新发明的形状，仓库里已经有两个现成的先例：

**先例 1（C 侧后台任务）**：`firmware/components/board_hal/board_hal.c` 的后台音频
- `xTaskCreatePinnedToCore(bg_audio_task, "bg_audio_task", 6144, NULL, 5, &h, 1)`（:727）
- 调用方 `xQueueSend(s_bg_audio_queue, &cmd, pdMS_TO_TICKS(50))`（:743）
- 任务里 `xQueueReceive(s_bg_audio_queue, &cmd, portMAX_DELAY)`（:614）
- 非阻塞探测 `uxQueueMessagesWaiting(s_bg_audio_queue) > 0`（:689）

**先例 2（Rust 侧 job 状态机）**：`WifiBackend` 的 `scan_start()` / `scan_state()` / `scan_results()` —— `ScanState::Idle/Scanning/Done/Error` **本身就是一个 job 状态机**（`vendor/pomelo-hal/src/types.rs`）。

⇒ HTTP 照抄这两者即可，**并且符合 `260926-03 §3.1` 已经写下的约定**：「长耗时/异步操作一律采用**发起 + 轮询状态**模式，由 FreeRTOS 任务/事件回调更新原子状态，Rust 侧查询永不阻塞」。

```text
板级 C：firmware/components/board_hal/board_http.c
  hal_http_start(url)      → xQueueSend 给 http_task（阻塞的 esp_http_client / TLS 在任务里）
  hal_http_state()         → 读任务写的原子状态（非阻塞）
  hal_http_take(buf,len)   → 非阻塞取结果
  hal_http_cancel()

Rust 接口：vendor/pomelo-hal/src/traits/http.rs（与 WifiBackend 同形，不引入 async）
  fn start(&mut self, url: &str) -> Result<(), HalError>;
  fn state(&self) -> DownloadState;      // Idle / Running(done,total) / Done / Error
  fn take_result(&mut self) -> Result<Vec<u8>, HalError>;
  fn cancel(&mut self) -> Result<(), HalError>;

实现：设备 firmware/pomelo-hal-esp32/src/http.rs（薄 FFI 封装）
      桌面 vendor/pomelo-hal/src/sim/http.rs（std::thread + 可配置的慢速/失败/超时）

app：board.http().start(url)  +  Subscription(time::every(100ms)) → Message::Poll
     → state() == Done 时 take_result()
```

**HAL 的"同步快照"哲学一个字都不用改，async 也不需要。**

---

## 六、 落地清单

1. `vendor/pomelo-hal`：新增 `HttpBackend`（start / state / take_result / cancel）+ `SimHttp`（桌面线程 + 模拟慢速、失败、超时）+ 单元测试。
2. `firmware/components/board_hal/board_http.c` + `firmware/pomelo-hal-esp32/src/http.rs`：
   - `esp_http_client`；**已就位**：`CONFIG_ESP_HTTP_CLIENT_ENABLE_HTTPS=y`（`firmware/sdkconfig:1463`）、`CONFIG_ESP_TLS_USING_MBEDTLS=y`（:1141）。
   - **HTTPS 不用自备 CA**：`CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y`（全量 Mozilla）已经开着（:3201）；但证书有效期校验需要**真实的系统时间**，那一步还没做 —— 见 `260929-04`。
   - 任务栈给 8~16 KB（音频任务才 6 KB）。
3. `vendor/iced-pomelo-gfx`：bitmap 图元（RGB565 blit）+ damage 支持。
4. 新增 `Image` widget（`Arc<[u16]>` + 尺寸），照 `260929-01 §步骤 1` 的共享 widget 约定（`vendor/pomelo-widgets`，一个 widget 一个模块）。
5. 解码：C 侧 `esp_jpeg`，或一个轻量 Rust JPEG decoder，**跑在 worker 线程里**。

---

## 七、 必须实测 / 确认的点

- **内部 SRAM**：TLS 握手 + 下载期间的 `heap`（`esp_wifi` 已经吃掉一块，mbedTLS 又要一块）。
- **解码耗时**：480×480 JPEG 在设备上的实际耗时 → 决定要不要先显示"加载中"占位。
- **全屏 bitmap 的一次性呈现成本**：预期 30~60 ms（整屏 flush 之前实测为 31.9 ms）。一次性加载可以接受；动画/视频则完全不可能。
- **`panic = "abort"`**（`firmware/components/rust_main/Cargo.toml` 的 `[profile.release]`）⇒ worker 线程里 panic 会直接重启整机：**所有错误必须变成值**（`DownloadState::Error`）。
- **任务栈大小**：音频任务 6 KB，TLS 需要更多。

---

## 八、 待办清单 (TODO)

- [ ] 新增 `HttpBackend` trait + `SimHttp`（桌面可测：慢速 / 失败 / 超时 / 取消）
- [ ] `board_http.c`（`esp_http_client` + FreeRTOS 任务 + 队列）与 `EspHttp` 薄封装
- [ ] HTTPS：证书包已经在，缺的是系统校时（`260929-04`）
- [ ] `pomelo-gfx` 新增 bitmap 图元（RGB565）+ damage
- [ ] `Image` widget（`Arc<[u16]>`）+ 一个演示页（如"下载并显示一张图"）
- [ ] 解码方案选型（C 侧 `esp_jpeg` vs Rust 轻量 decoder），并放到 worker 线程
- [ ] 真机实测：TLS 内存、解码耗时、全屏呈现成本
