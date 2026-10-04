# 待办规划: HTTPS / TLS 支持 (HTTPS & TLS Support)

- **模块**: `firmware/sdkconfig` / `firmware/components/board_hal` / `firmware/pomelo-hal-esp32` / `vendor/pomelo-hal` / `apps/*`
- **硬件环境**: ESP32-S3（双核 LX7 @ 240MHz，8 MB PSRAM，16 MB Flash，当前镜像 7.24 MB / 8 MB）
- **状态**: 客户端与证书包**已经在镜像里**；缺的是"系统校时"这一次接线，以及真机验证
- **关联**: `260929-03`（阻塞 IO 与图片通路 —— `HttpBackend` 的落地形状）、`260929-01 §五-2`（状态栏时钟来源，与本篇是同一件事）、`260929-02`（HAL 不引入 async）、`260926-03 §3.1`（「发起 + 轮询状态」）

---

## 一、 结论摘要

1. **不用换库，也不用自己准备 CA。** IDF 的 `esp_http_client` + `esp-tls` + mbedTLS 已经编进镜像，**Mozilla 全套根证书也已经开着**（`CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y`、`_DEFAULT_FULL=y`、`MAX_CERTS=200`）。所以"支持 HTTPS"本身**不需要写代码**：调 `esp_http_client` 时带上 `esp_crt_bundle_attach` 即可（那也是默认）。

2. **真正会挡住你的是系统时间。** mbedTLS 会校验证书的 `notBefore`/`notAfter`，而**当前没有任何代码给系统校时**（SNTP 编进去了，但没有人调用它；`firmware/` 里 `grep sntp` 为空）。没有正确的时间，所有公网证书都是"尚未生效"，握手会以证书校验失败告终 —— 一个和 HTTPS 看起来毫不相关的地方。

3. **这件事和状态栏时钟是同一件事。** `260929-01 §五-2` 正在问"电池/Wi-Fi 改读板子之后，时钟从哪来"。一次做完：**Wi-Fi 拿到 IP → SNTP → 系统时间 → 状态栏显示真实时间 + HTTPS 可用**，顺带把固件那条 `Message::Status` 推送退休。

---

## 二、 现状（核实过的 sdkconfig 事实）

| 项 | 配置 | 位置 | 说明 |
| :--- | :--- | :--- | :--- |
| HTTP 客户端 | `CONFIG_ESP_HTTP_CLIENT_ENABLE_HTTPS=y` | `firmware/sdkconfig:1463` | `esp_http_client` 已编入 |
| TLS 栈 | `CONFIG_ESP_TLS_USING_MBEDTLS=y` | `:1141` | 走 mbedTLS |
| 传输缓冲 | `CONFIG_ESP_TLS_DYN_BUF_STRATEGY_SUPPORTED=y` | `:1159` | esp-tls 的传输缓冲可动态 |
| **根证书包** | `CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y` + `_DEFAULT_FULL=y` + `MAX_CERTS=200` | `:3201` / `:3207` / `:3217` | **Mozilla 全量，无需自备 CA** |
| 内容缓冲 | `ASYMMETRIC_CONTENT_LEN=y`，IN **16 KB** / OUT **4 KB** | `:3152` / `:3154` / `:3156` | 每条连接一份 |
| 动态缓冲 | `CONFIG_MBEDTLS_DYNAMIC_BUFFER` **未开** | `:3158` | 缓冲区静态，连接建立即分配 |
| 协议版本 | TLS **1.2**（1.3 未开） | `:3233` / `:3235` | 绝大多数站点没问题；只支持 1.3 的连不上 |
| 硬件加速 | `HARDWARE_SHA` / `HARDWARE_MPI` / `HARDWARE_AES` | `:3414` / `:3416` / `:3424` | 握手不会慢 |
| 对端证书 | `SSL_KEEP_PEER_CERTIFICATE` 未开 | `:3249` | 省内存 |
| SNTP | `LWIP_SNTP_MAX_SERVERS=1`、`UPDATE_DELAY=3600000`、`STARTUP_DELAY=y` | `:3022` / `:3026` / `:3028` | **已编译进来，但没有任何调用** |
| 时间来源 | `ESP_TIME_FUNCS_USE_RTC_TIMER=y`、`NEWLIB_TIME_SYSCALL_USE_RTC_HRT=y` | `:2071` / `:5151` | 上电后 `time()` 从 RTC 起算 ⇒ **未校时 ≈ 1970** |
| netif | `esp_netif_init()` + `esp_netif_create_default_wifi_sta()` | `firmware/components/board_hal/board_wifi.c:187` / `:196` | SNTP 需要的 netif 已经有了 |

> 记录一个教训：第一次查这份配置时用了 `grep … | head -12`，证书包那几行（在 `:3201` 之后）被截掉了，我据此得出"没有开启"的**反结论**。截断的 grep 会给出确定的错误答案。

---

## 三、 缺口一：系统时间与证书有效期

- **机制**：mbedTLS 的 `x509_crt_verify` 拿系统时间检查有效期。未校时时 `time()` 从 RTC 起算 ⇒ 约等于 1970，**早于所有公网证书的 `notBefore`** ⇒ `MBEDTLS_X509_BADCERT_FUTURE` ⇒ esp-tls 报 `X509_CERT_VERIFY_FAILED`。
- **现状**：`firmware/` 里没有任何 SNTP / `settimeofday` 调用（只有 `managed_components` 里第三方示例代码提到 `time(NULL)`）。SNTP 客户端本身是好的（lwIP 的选项都在），只是没人启动它。
- **做法**：拿到 IP 之后启动一次 `esp_netif_sntp_init(&cfg)`（默认 `pool.ntp.org`，见 §五-1），`esp_netif_sntp_sync_wait(5s)`。**未同步前 HTTPS 一律拒绝**，并且在 API 层给出明确错误（`Error::ClockNotSet`），而不是让用户看到一句笼统的"连接失败"。
- **顺带收益**：状态栏时钟（`260929-01 步骤 3`）同时解决，`Message::Status` 那条"平台推时钟"的通道可以删掉。
- **不采纳的反面选项**：`CONFIG_ESP_TLS_INSECURE`、`skip_common_name`、跳过时间校验 —— 那等于把 HTTPS 的意义丢掉，只配当调试开关。

---

## 四、 缺口二：预算（都要实测）

- **Flash**：证书包**只在被引用时才进镜像**（`x509_crt_bundle` 由 `esp_crt_bundle_attach` 引用）。今天没人用 TLS ⇒ 它现在很可能**根本不在镜像里**。第一次用 HTTPS 会多出约 **64 KB** 量级（200 张全量 bundle）。当前剩 ~0.76 MB，装得下，但要记在账上，别把它当回归。
- **内部 SRAM vs PSRAM**：`MALLOC_ALWAYSINTERNAL=0` ⇒ 这些缓冲默认落 **PSRAM**，不抢 `esp_wifi` 需要的内部 SRAM；代价是硬件 AES/SHA 读 PSRAM 会慢一点。每条连接：内容缓冲 16 KB + 4 KB，握手临时内存 10 KB 级。证书是**按需解析**，RAM 占用跟连接数走、不是 200 张一起进内存（**需实测确认**）。
- **时间**：握手 1–3 s 量级（240 MHz + 硬件加速），SNTP 首同步再几秒。这正好落在 `260929-03 §五` 的「任务 + 非阻塞 poll」形状里：UI 显示"连接中"，帧循环照旧。

---

## 五、 要拍板的决策

1. **NTP 服务器**：`pool.ntp.org` / 国内公共源 / 我们自己的服务器（后者最稳，且不依赖外网可达性）。
2. **未校时时的策略**：拒绝 HTTPS 并报明确错误（推荐） / 允许但只跑明文 `http` / 跳过校验（不推荐）。
3. **是否开 TLS 1.3**（`:3235`）：换来对少数站点的兼容，代价是 flash 与握手内存。
4. **是否开 `CONFIG_MBEDTLS_DYNAMIC_BUFFER`**（`:3158`）：省常驻内存，代价是每次连接的分配开销。
5. **是否做证书 pinning**（自建服务器 + 固定公钥）：能关掉整包省 flash、也更防中间人，代价是服务端换证书要发固件。
6. **时间的用法**：NTP 同步会让墙上时间**跳变** ⇒ 定时器/动画一律用 `Instant` 单调时钟，不要用 `SystemTime`。

---

## 六、 实施步骤（接在 `260929-03 §六` 之后）

1. **C 侧 `board_http.c`**：`esp_http_client` + `esp_crt_bundle_attach`（默认即是），把结果写进队列（见 `260929-03 §五`）。
2. **C 侧 `hal_time_*`**：`esp_netif_sntp_init()`（拿到 IP 后调用一次）、`hal_time_now_unix()`、`hal_time_is_synced()`。
3. **HAL 侧 `TimeBackend`**：同步快照形（`now_unix()` / `synced()`），与 `WifiBackend` 同形 —— **不引入 async**（`260929-02`）。
4. **`HttpBackend::start()`**：`!synced()` 时返回 `Error::ClockNotSet`，把这条依赖变成显式错误。
5. **状态栏**：改读 `board.time().now_unix()`（与 `260929-01 步骤 3` 合并），删掉固件的 `Message::Status` 推送。
6. **真机验证**（见 §七）。

---

## 七、 真机验证清单

- [ ] 连上 Wi-Fi 后打印一次 `time()`，确认回到真实时间
- [ ] SNTP 首次同步耗时；断网重连 / 换网后是否重新同步
- [ ] 一次 `https://` GET（小文件）成功
- [ ] **反向验证时钟这条链**：故意不校时（或把时间设回 1970）⇒ 必须是明确的 `ClockNotSet`/证书失败，而不是"神秘连不上"
- [ ] 握手的实际耗时，以及 TLS 前后 `heap` 快照（PSRAM / 内部各多少）
- [ ] 同时播放音乐 + TLS 下载时是否掉帧
- [ ] 换一个站点（不同 CA / 不同证书链）仍能连

---

## 八、 风险与陷阱

- **未校时会被伪装成"网络失败"** —— 这也是为什么第 4 步要把它在 API 层变成显式错误。
- **只支持 TLS 1.2**（`:3235` 未开），个别站点会连不上；先不要急着开 1.3，等遇到再说。
- **首次引入 HTTPS 会让镜像涨 ~64 KB**（证书包开始被链接），不是回归。
- **时间跳变**：见 §五-6。
- **`esp_wifi` 与 TLS 同时要资源**：内部 SRAM 与 PSRAM 带宽两条都要实测。

---

## 九、 待办清单 (TODO)

- [ ] 决定 NTP 源与"未校时"策略（§五-1 / §五-2）
- [ ] C 侧 `hal_time_*`（`esp_netif_sntp_init` + 两个查询）
- [ ] HAL 侧 `TimeBackend` + `SimTime`（桌面可测）
- [ ] `HttpBackend::start()` 的 `ClockNotSet` 前置检查
- [ ] `board_http.c` 的 `esp_crt_bundle_attach`
- [ ] 状态栏改读真实时间，删掉 `Message::Status` 推送
- [ ] 真机跑完 §七 的清单（含反向验证）
