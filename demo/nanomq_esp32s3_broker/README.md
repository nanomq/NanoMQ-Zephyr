# NanoMQ broker on ESP32-S3 (esp32s3_devkitc/esp32s3/procpu)

Runs the full NanoMQ broker core (`nanomq/nanomq/` application sources +
the NanoNNG submodule `nng/`) on an **ESP32-S3** over **Wi-Fi (STA) +
DHCP**, demonstrated on the Espressif **ESP32-S3-LCD-EV-Board** with an
ESP32-S3-WROOM-1-N16R16V module (16 MB flash + 16 MB octal PSRAM).

Sibling of [demo/zephyr_broker](../zephyr_broker/) (qemu_x86): same app
sources and NanoNNG ExternalProject build, different board/networking
layer.  Feature surface here matches the qemu demo: MQTT over TCP (:1883) +
REST API (:8081) + MQTT over WebSocket (:8083/mqtt) — webhook and the DEBUG
log stay off.

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
`wifi: connected` → DHCP (`net: ipv4 192.168.1.x`) → SNTP seed
(`sntp: ntp.aliyun.com: epoch=…, realtime seeded`) → broker banner +
REST listener.

Log timestamps are real wall-clock **UTC**: the board has no RTC, so once
DHCP has bound, main.c's `seed_realtime_from_sntp()` queries a public SNTP
server and seeds `CLOCK_REALTIME` (nanolib's log module formats
`time(NULL)`, so a correct system clock is all the timestamps need).
Zephyr has no timezone database, so what is displayed is always UTC.  The
seed is best effort — if no server answers (~9 s worst case) the broker
still starts, just with the 1970 epoch.  Background, and why Zephyr's
ready-made `net_init_clock_via_sntp()` helper was *not* reused:
PORTING_ZEPHYR.md §22-4.

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

## Verified on hardware — full MQTT surface (2026-09-10)

Client traffic now passes on the ESP32-S3-LCD-EV-Board:

* MQTT v3.1.1 QoS 0/1/2 publish→subscribe round trips, retained
  messages, REST API, and connect/disconnect churn all verified with
  mosquitto against the board at `192.168.1.10`.
* The earlier client-CONNECT heap corruption was an **allocator-family
  mismatch** in the MQTT codec (`mqtt_codec.c` freed `nni_zalloc`'d
  proto-data/properties with libc `free()`, plus two `nng_zalloc` ↔
  `free` sites in `mqtt_qos_db.c`) — invisible upstream where the nng
  allocator *is* libc malloc, corrupting both heaps here.  Fixed in
  nng `cb34268`, bumped by nanomq `7ae89a2b`; full forensic record in
  PORTING_ZEPHYR.md §22-3(b).
* Companion fixes on the way: nanolib topic-queue pairing (nng
  `80cf26b`), webhook cJSON free (nanomq `84fd90f6`).

Run the suite against the board with:

```sh
python3 demo/zephyr_broker/function_test.py --no-manage --addr <board-ip> \
    --group mqtt_v311 --group mqtt_v5 --group rest_get
```

The runner tunes itself to the hardware: it measures the TCP round trip to the
broker and, when that says "not localhost" (loopback and the container bridge
are both under ~1 ms; this board measures 14–600 ms over Wi-Fi), defaults to
`--time-scale 4 --retry 2`.  Both are needed — the stretched scale covers the
CI scripts' localhost-tuned sleeps, and the retry covers two of their subtests
that are racy by construction.  An explicit `--time-scale`/`--retry` always
wins; the values actually used are printed at startup
(PORTING_ZEPHYR.md §22-3(e)/(g)).
