# NanoMQ broker on ESP32-S3 (esp32s3_devkitc/esp32s3/procpu)

Runs the full NanoMQ broker core (`nanomq/nanomq/` application sources +
the NanoNNG submodule `nng/`) on an **ESP32-S3** over **Wi-Fi (STA) +
DHCP**, demonstrated on the Espressif **ESP32-S3-LCD-EV-Board** with an
ESP32-S3-WROOM-1-N16R16V module (16 MB flash + 16 MB octal PSRAM).

Sibling of [demo/zephyr_broker](../zephyr_broker/) (qemu_x86): same app
sources and NanoNNG ExternalProject build, different board/networking
layer.  Feature surface here: MQTT over TCP (:1883) + REST API (:8081) —
WS/webhook/DEBUG log stay off.

## Board target

ESP32-S3-LCD-EV-Board has no Zephyr board of its own; the demo builds on
the SoC-identical `esp32s3_devkitc/esp32s3/procpu`.  Module differences
(16 MB flash + PSRAM) are carried by the app overlay
[boards/esp32s3_devkitc_procpu.overlay](boards/esp32s3_devkitc_procpu.overlay)
(16 MB flash + 16 MB octal PSRAM for the N16R16V module — edit to match
yours).

## Why PSRAM (memory layout)

The qemu demo's broker data plane is several MB; ESP32-S3 internal SRAM
is ~512 KB (416 KB usable), so the demo lives off the PSRAM:

* `CONFIG_ESP_SPIRAM=y` + `SPIRAM_MODE_OCT`: Zephyr's octal-PSRAM support.
* The linker reserves a 4 MB heap window inside the PSRAM mapping
  (`CONFIG_ESP_SPIRAM_HEAP_SIZE`), registered as a shared multi-heap
  region by `soc/espressif/common/esp_psram.c`.
* The **NanoNNG Zephyr allocator is switched to that window** via
  `NNG_ZEPHYR_ALLOC_SMH` in [CMakeLists.txt](CMakeLists.txt) — but it
  uses a **plain `struct k_heap`** over the window rather than
  `shared_multi_heap_alloc()`: the latter is not thread-safe (bare
  sys_heap, no lock) and nng allocates from many threads (poller, taskq
  workers, per-connection aios) — unsynchronized smh access corrupted the
  heap at the first client connect.  See the allocator change in the nng
  submodule (`src/platform/zephyr/zephyr_alloc.c`).
* Static pools that never need SRAM are relocated into PSRAM:
  `CONFIG_ESP32_WIFI_NET_ALLOC_SPIRAM` (net stack + wifi driver .noinit)
  and `CONFIG_ESP_SPIRAM_BSS_RELOC_LIBS_AND_OBJS` (Zephyr POSIX object
  pools ≈60 KB and the net_buf pools).

Final footprint (linker report): FLASH ~958 KB, internal SRAM
`dram0_0_seg` ~310 KB / 399 KB (77 %), PSRAM window ~5.2 MB (incl. the
4 MB broker heap).

## Environment / prerequisites

* A Zephyr ≥ 4.4 west workspace (`esp-zephyr` alias per the repo guide:
  ESP-IDF venv + `ZEPHYR_SDK_INSTALL_DIR`, `unset ZEPHYR_TOOLCHAIN_VARIANT`).
* Espressif HAL **blobs** must be fetched once — without them
  `CONFIG_WIFI_ESP32` stays silently hidden and the build has no Wi-Fi:
  ```sh
  west blobs fetch hal_espressif
  ```

## Build

```sh
west build -b esp32s3_devkitc/esp32s3/procpu demo/nanomq_esp32s3_broker \
    -- -DEXTRA_CONF_FILE=local.conf     # Wi-Fi credentials
```

Without `-d` the build lands next to the app
(`demo/nanomq_esp32s3_broker/build/`, git-ignored) and `west flash` from
the repo root picks it up.  The bring-up record was produced with an
explicit `-d` into the west workspace instead; pass the same directory to
both commands:

```sh
west build -b esp32s3_devkitc/esp32s3/procpu \
    -d /path/to/ZephyrProject/build/esp32s3_nanomq \
    demo/nanomq_esp32s3_broker -- -DEXTRA_CONF_FILE=local.conf
```

`local.conf` is git-ignored; start from `local.conf.example`.  Wi-Fi
credentials live in `CONFIG_BROKER_WIFI_SSID/PSK` (app Kconfig) and never
land in the tree.

## Flash & run

```sh
west flash -d <the same build dir you built into> \
    --runner esp32 --esp-device /dev/ttyUSB0
# serial console (115200): idf-monitor / miniterm
```

`-d` is relative to the current directory unless absolute — the board is
not found (`... is not a directory`) when pointing at a build dir that
does not exist, e.g. repo-relative `build/...` while the workspace-level
dir is what was built.

Boot sequence on the console: PSRAM chip init + memory test → Zephyr →
`wifi: connected` → DHCP (`net: ipv4 192.168.1.x`) → broker banner +
REST listener.  Log timestamps show `1970-01-01` — the board has no RTC
and no time source is wired (cosmetic).

## Verified on hardware (bring-up record)

* 16 MB octal PSRAM detected & memory-tested (80 MHz).
* Wi-Fi STA on a 2.4 GHz WPA2 AP, DHCP lease, broker listening on
  :1883 / REST :8081 (curl-able).
* Several portability fixes landed on the way (all in this repo or the
  nng submodule):
  * shared ExternalProject cmake: `-mno-movbe` only for x86;
    `NNG_ZEPHYR_NO_STDATOMIC` for every 32-bit non-x86 target
    (xtensa lacks 64-bit atomics);
    `BUILD_ALWAYS` so nng source edits actually rebuild.
  * net_mgmt: one callback per event — OR'ing different layer-codes
    into one `net_mgmt_init_event_callback` mask silently drops every
    delivery (`mgmt_run_slist_callbacks` compares whole layer-code).
  * wifi driver bring-up knobs in prj.conf comments (STA auto-DHCP /
    auto-reconnect interplay with an app-side connect flow).

## Known issue — client CONNECT still fails (open)

**Status: partially fixed, one item open.**  Client CONNECT crashed the
broker in the PSRAM heap.  Root cause (2026-09-10) was a family of
**allocator-family mismatches** in nanolib/nanomq: objects allocated with
the nng allocator but freed with libc `free()` (or the reverse).  That is
invisible upstream — POSIX nng's allocator *is* libc malloc — but on this
port the nng allocator is the PSRAM k_heap, so each mismatch corrupts one
of the two heaps.  Fixed here: mqtt_db.c / hash_table.c / mqtt_parser.c
(nng 80cf26b) and webhook_post.c (nanomq 84fd90f6).

Remaining: the CONNECT `client_status` event is processed **twice** on
Zephyr, so the same `pub_packet` is torn down twice
(`server_cb → free_pub_packet → k_heap_free`, wild write).  Host builds
take a single pass (hence clean ASAN/glibc runs).  Forensics and next
steps: PORTING_ZEPHYR.md §22-3(b).  Until that path is fixed, MQTT
client traffic on this board still panics the broker; build/flash/boot/
Wi-Fi/DHCP/REST are verified.
