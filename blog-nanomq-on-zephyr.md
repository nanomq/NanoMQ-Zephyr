## 引言

MQTT 与 Zephyr 的组合通常出现在客户端侧：Zephyr 设备作为 MQTT 客户端，连接到远端 broker。本文讨论的是另一个方向——**将 broker 本身运行在 MCU 上**。

这一需求来自边缘侧的典型场景：现场数十个设备使用 Modbus、串口或私有协议，需要统一收敛为 MQTT；或者现场网络不稳定，数据需要先在本地缓存，待链路恢复后再上传。常规方案是在设备旁部署一台 Linux 网关，但其供电、维护与防护成本往往不划算。若 broker 能直接运行在既有的 MCU 上，这一层即可省去。

[NanoMQ](https://nanomq.io/) 是 EMQX 旗下的开源边缘 MQTT Broker，原本面向 Linux 等 POSIX 环境。本文所述的移植将 broker 核心运行于 Zephyr RTOS：应用层仍是同一份 `nanomq/` 源码加上 NanoNNG 的 MQTT 协议栈，未做裁剪；外围能力则依 Zephyr 平台的实际条件禁用或尚未验证——完整的边界清单见[附录 A](#附录-a与通用版-nanomq-的差异)。仓库包含两个 demo，分别运行于 ESP32-S3 实机与 `qemu_x86` 模拟环境，两者的对比与选择见[附录 B](#附录-b两个-demo-的对比)。

## Zephyr 简介

Zephyr 是由 Linux Foundation 托管的开源实时操作系统（RTOS），面向资源受限的嵌入式设备。与本移植相关的特性主要有四点：

- **可裁剪的配置系统**。系统功能通过 Kconfig 选项逐项启用，最小配置可压缩至数十 KB，因此同一套代码可以覆盖从传感器节点到网关的不同量级。
- **设备树描述硬件**。板级差异由 devicetree 与 board overlay 表达，应用代码不感知具体板卡，这也是本移植能够用同一份源码同时构建 `qemu_x86` 与 ESP32-S3 的前提。
- **自带网络协议栈**。包含 TCP/IP、BSD 兼容的 socket 层以及 TLS 等，nng 的 Zephyr 平台层正是建立在这套 socket API 之上。
- **POSIX 子集**。提供 `CONFIG_POSIX_API` 覆盖 pthread、文件描述符等接口，使原本面向 POSIX 的中间件具备迁移的可能。

本文基于 Zephyr 4.4（`11a87708d41`）。

## NanoMQ 简介

[NanoMQ](https://nanomq.io/) 是 EMQX 旗下的开源 MQTT Broker，属于 LF Edge 项目，官方定位为「面向 IoT 边缘的超轻量、高速 MQTT Broker」。与面向云端的 EMQX 不同，NanoMQ 面向嵌入式与工业现场设计。

**架构。** NanoMQ 采用分层设计，自下而上为：

- **平台适配层**：探测硬件与操作系统，向上提供兼容 API，避免绑定特定平台——这也是本次移植能够仅通过新增一个平台层来完成的基础。
- **任务层**：内置 Actor 模型与线程级并行，可在 SMP 系统上横向扩展。
- **传输层**：按 pipe / client 管理 TCP、UDP 流，采用 zero-copy 降低内存占用。
- **协议层**：将字节流解析为 MQTT 报文，生成事件并维护 in-flight 窗口。
- **应用层**：对接 rule 引擎与全局主题树，以无锁状态机向用户暴露 MQTT 消息与事件。

**性能。** [NanoMQ 官方](https://nanomq.io/)公布的指标包括：最小功能集下启动占用低于 200 KB；百万级 TPS；在多核 CPU 上相较 Mosquitto 快至 10 倍。

需要强调这些指标的适用范围：它们出自**多核 POSIX 环境**的 benchmark，官方页面未给出对应测试的硬件与配置细节，也**不代表本文 ESP32-S3 demo 的性能**——Zephyr 侧的线程配额远小于多核 Linux，实际并发能力与此不同，详见[附录 A](#附录-a与通用版-nanomq-的差异)。本文未对 demo 做正式的性能测试。

**实现。** NanoMQ 为纯 C 实现，无外部运行时依赖，便于交叉编译到各类目标板。其异步 I/O 与 Actor 架构构建在 nanomq 组织维护的 NNG fork（[NanoNNG](https://github.com/nanomq/NanoNNG)）之上，并在其上扩展了 MQTT 协议实现与 nanolib 工具库。协议层面完整支持 MQTT 3.1.1 / 3.1 与 MQTT 5.0，包括 QoS 0/1/2、保留消息、遗嘱消息、共享订阅、主题别名等特性。

## 环境搭建

### 依赖

- Zephyr ≥ 4.4 的 west workspace
- Zephyr SDK 1.0.x（需单独下载，解压后将 `ZEPHYR_SDK_INSTALL_DIR` 指向该目录）
- ESP32-S3 实机 demo 另需 Espressif 的 xtensa 工具链（使用 ESP-IDF 提供的环境即可）
- 运行功能测试另需 Python 依赖与 mosquitto 客户端，见「运行功能测试」一节

本文的验证环境固定到以下版本。其中 e1000 补丁只适用于对应的 Zephyr 源码版本，升级 Zephyr 后需重新确认：

| 组件 | 版本 / 提交 |
|---|---|
| Zephyr | 4.4，`11a87708d41`（e1000 补丁针对此版本） |
| Zephyr SDK | 1.0.1 |
| NanoMQ | 0.25.1 |
| NanoNNG（`nng/` 子模块） | `ad72510`（`git clone --recursive` 自动检出） |

workspace 尚未搭建时：

```sh
west init ~/zephyrproject
cd ~/zephyrproject
west update
```

### ESP32-S3 开发环境

只跑 `qemu_x86` 的话可以跳过本节。实机 demo 需要额外的 xtensa 工具链与 Espressif 的 HAL：

**1. 获取 ESP-IDF**（提供 xtensa 工具链与 esptool）：

```sh
git clone --recursive https://github.com/espressif/esp-idf.git ~/esp/esp-idf
cd ~/esp/esp-idf && ./install.sh esp32s3
```

**2. 每次构建前激活环境**，并把 `ZEPHYR_SDK_INSTALL_DIR` 指向 Zephyr SDK：

```sh
source ~/esp/esp-idf/export.sh
export ZEPHYR_SDK_INSTALL_DIR=$HOME/zephyr-sdk-1.0.1
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
```

写入 `~/.bashrc` 可以省去每次手动执行。`ZEPHYR_TOOLCHAIN_VARIANT` 也可以不设，让 Zephyr 自动探测 ESP-IDF 环境。

**3. 拉取 Espressif HAL blobs**（在 workspace 内执行一次）：

```sh
cd ~/zephyrproject
west blobs fetch hal_espressif
```

**这一步不能跳过**：若未拉取，`CONFIG_WIFI_ESP32` 会静默变为不可见——构建照常成功、固件照常启动，只是没有 Wi-Fi，且构建系统不会给出任何警告。表现为串口上始终等不到 `wifi: connected`。

更详细的发行版相关步骤（系统依赖、SDK 安装、权限与常见报错）见 [demo/nanomq_esp32s3_broker/setup-fedora-zh.md](demo/nanomq_esp32s3_broker/setup-fedora-zh.md)（以 Fedora 为例，其他发行版替换包管理器即可）。

### 获取源码

```sh
cd ~/zephyrproject
git clone --recursive https://github.com/nanomq/NanoMQ-Zephyr.git
cd NanoMQ-Zephyr
```

`--recursive` 会同时拉取 `nng/` 子模块（即包含 Zephyr 平台层的 NanoNNG 分支）。若克隆时未加 `--recursive`，可补执行：

```sh
git submodule update --init --recursive
```

后续命令均在本仓库根目录（`~/zephyrproject/NanoMQ-Zephyr`）执行。

### qemu_x86：Zephyr e1000 驱动补丁

使用 `qemu_x86` demo 需要为 Zephyr 打一个两行补丁。**该补丁为必需项**：未打补丁时 broker 会正常启动并输出 `NanoMQ Broker is started successfully!`，但任何客户端都无法建立连接。

原因是 QEMU 的 e1000 设备模型在复位后会清空 RCTL 寄存器，其中包括 **RCTL_BAM**（Broadcast Accept Mode，bit 15）——真实硬件默认置位，而设备模型不置位。Zephyr 的 `eth_e1000` 驱动只写入 `RCTL_EN | RCTL_MPE`，从不设置 BAM，导致**所有广播帧被设备模型静默丢弃**，ARP 无法完成解析，SLIRP 也就无法递交第一个 TCP 连接。上游 Zephyr 截至 4.4（`11a87708d41`）仍存在该问题。

补丁打在 **Zephyr 自身的源码树**（workspace 下的 `zephyr/` 目录，不是本仓库）。将下面的 diff 存为 `zephyr-e1000-bam.patch` 后应用：

```sh
cd ~/zephyrproject/zephyr
git apply /path/to/zephyr-e1000-bam.patch
```

```diff
diff --git a/drivers/ethernet/eth_e1000.c b/drivers/ethernet/eth_e1000.c
--- a/drivers/ethernet/eth_e1000.c
+++ b/drivers/ethernet/eth_e1000.c
@@ -329,7 +329,7 @@ static const struct ethernet_api e1000_api = {
 									\
 		irq_enable(DT_INST_IRQN(inst));				\
 		iow32(dev, CTRL, CTRL_SLU); /* Set link up */		\
-		iow32(dev, RCTL, RCTL_EN | RCTL_MPE | DT_INST_PROP(inst, rdmts) << RDMTS_OFFSET); \
+		iow32(dev, RCTL, RCTL_EN | RCTL_MPE | RCTL_BAM | DT_INST_PROP(inst, rdmts) << RDMTS_OFFSET); \
 		iow32(dev, ITR, DT_INST_PROP(inst, itr) & (uint32_t)GENMASK(15, 0)); \
 	}								\
 									\
diff --git a/drivers/ethernet/eth_e1000_priv.h b/drivers/ethernet/eth_e1000_priv.h
--- a/drivers/ethernet/eth_e1000_priv.h
+++ b/drivers/ethernet/eth_e1000_priv.h
@@ -27,6 +27,7 @@ extern "C" {
 #define IMS_RXT0	(1 << 7) /* Receiver Timer */
 
 #define RCTL_MPE	(1 << 4) /* Multicast Promiscuous Enabled */
+#define RCTL_BAM	(1 << 15) /* Broadcast Accept Mode */
 
 #define TDESC_EOP	     (1) /* End Of Packet */
 #define TDESC_RS	(1 << 3) /* Report Status */
```

注意 `RCTL_MPE` 是 **Multicast** Promiscuous Enabled（bit 4），只影响组播，与广播接收无关——驱动已有的这一位不能替代 BAM，这正是该问题长期未被发现的原因。

该补丁仅影响 qemu 的模拟网卡，ESP32-S3 实机 demo 无需应用。

### 端口与服务

两个 demo 对外暴露的监听面一致：

| 服务 | 端口 | ESP32-S3 | qemu_x86 | 认证 / 加密 |
|---|---:|---|---|---|
| MQTT over TCP | 1883 | 支持 | 支持 | 无认证，无 TLS |
| REST API | 8081 | 支持 | 支持 | Basic 认证（`admin` / `public`），无 TLS |
| MQTT over WebSocket | 8083 | 支持 | 支持 | 无认证，无 TLS（路径 `/mqtt`） |

Webhook 未在上表：它不对外监听，而是主动向外部接收器（`hook_receiver.py`，默认 `:18080`）POST 事件 JSON。**两个 demo 都默认关闭**，只在需要验证 webhook 时开启——原因见下文的实测数据：规则 `CLIENT_CONNACK` 对每次客户端连接都会触发一次 POST，开着会拖慢计时敏感的测试组。

### 安全前提

两个 demo 的默认配置都以**受控实验环境**为前提，直接照搬会带来风险：

| 项 | 现状 |
|---|---|
| 监听地址 | MQTT `nmq-tcp://0.0.0.0:1883`、WebSocket `nmq-ws://0.0.0.0:8083/mqtt`、REST `0.0.0.0:8081`，**均监听所有网卡** |
| REST 认证 | Basic 认证，默认 `admin` / `public`（与 `etc/nanomq.conf`、上游文档一致；凭据在 `main.c` 中设置，可改） |
| MQTT 认证 | 未启用，任何客户端均可连接、发布与订阅 |
| 传输加密 | 无。TLS 在 NanoNNG 的 Zephyr 移植中未启用，MQTT / WebSocket / REST 均为明文 |

需要特别说明：**Basic 认证在明文 HTTP 上只是 Base64 编码，不是加密。** 凭据可被同一链路上的嗅探者还原，因此它挡得住无意访问，挡不住有意攻击。真正的访问控制仍然依赖网络边界。

因此在实验网络之外使用前，至少需要：将监听地址收窄到特定网卡、更换 REST 默认凭据、为 MQTT 启用认证、并把管理接口置于可信网络之后或额外的 TLS 代理之后。**不要将 demo 的默认配置直接暴露到公网或不可信网络。**

## 编译与运行：ESP32-S3 实机

### 1. 配置 Wi-Fi 凭据

将 `local.conf.example` 复制为 `local.conf`（该文件已在 `.gitignore` 中，凭据不会进入仓库），并填入 SSID 与 PSK：

```sh
cp demo/nanomq_esp32s3_broker/local.conf.example \
   demo/nanomq_esp32s3_broker/local.conf
```

编辑 `demo/nanomq_esp32s3_broker/local.conf`：

```conf
CONFIG_BROKER_WIFI_SSID="your-ssid"
CONFIG_BROKER_WIFI_PSK="your-passphrase"
```

若要一并启用 webhook，在同一个文件里补上接收端地址。注意它必须是**运行接收器那台机器的局域网地址**（不是 `127.0.0.1`，也不是 qemu 的 `10.0.2.2`），因此换网络后需要重新编译；不填则 webhook 保持关闭：

```conf
CONFIG_BROKER_WEBHOOK=y
CONFIG_BROKER_WEBHOOK_URL="http://192.168.1.13:18080/"
```

**建议只在需要验证 webhook 时打开它。** 规则 `CLIENT_CONNACK` 对**每次客户端连接**都会触发一次 POST，接收器不在线时每次连接就多出一次失败的 HTTP 连接加两行同步日志（这块板子开了 `CONFIG_LOG_MODE_IMMEDIATE`）。实测:开着 webhook 跑一轮 `mqtt_v5` 出现了 **97 次**失败的 POST,而该组里那个按进程启动顺序决出胜负的竞态子测试,三轮全部失败;关掉后恢复为"重试后通过"。所以这里默认关闭,`prj.conf` 里也是关的。

### 2. 编译

`EXTRA_CONF_FILE` 相对 app 目录解析：

```sh
west build -b esp32s3_devkitc/esp32s3/procpu -d build/esp32s3_nanomq \
    demo/nanomq_esp32s3_broker -- -DEXTRA_CONF_FILE=local.conf
```

构建成功后输出的内存报告（本文验证的配置，已启用 MQTT/REST/WS/webhook）：

```
Memory region         Used Size  Region Size  %age Used
           FLASH:      962100 B   16776960 B      5.73%
     iram0_0_seg:       54028 B     415492 B     13.00%
     dram0_0_seg:      314008 B     399108 B     78.68%
     irom0_0_seg:      685168 B        32 MB      2.04%
     drom0_0_seg:      831028 B        32 MB      2.48%
    ext_dram_seg:     5152752 B        32 MB     15.36%
...
Successfully created ESP32-S3 image.
```

看点有两个：`dram0_0_seg`（内部 SRAM）已用 **78.68%**——这是最紧张的一块，也是为什么要把 broker 堆与静态池挪进 PSRAM；`ext_dram_seg` 的 5.2 MB 即 PSRAM 中被征用的映射区域。

### 3. 烧录

开发板通过 USB 连接主机后，会在系统中枚举出一个串口设备，设备名取决于是走板载 USB-UART 桥接芯片还是 USB-JTAG/Serial：

- `/dev/ttyUSB*` —— 独立桥接芯片（常见 CP2102、CH34x）；
- `/dev/ttyACM*` —— 芯片原生 USB CDC（ESP32-S3 的 USB-JTAG/Serial 即属此类）。

本文示例使用 `/dev/ttyUSB0`，实际设备名以下列命令为准：

```sh
ls /dev/ttyUSB* /dev/ttyACM*
# 或观察插入开发板时内核新增的设备节点
dmesg | tail -5
```

若烧录时提示权限不足，需要将当前用户加入串口所属的用户组（Debian/Ubuntu 为 `dialout`，Fedora/Arch 为 `uucp`），重新登录后生效：

```sh
sudo usermod -aG dialout $USER     # Fedora 用 uucp
```

确认后执行烧录（将 `/dev/ttyUSB0` 替换为实际设备名）：

```sh
west flash -d build/esp32s3_nanomq --runner esp32 --esp-device /dev/ttyUSB0
```

### 4. 查看串口

波特率 115200，可使用 `idf-monitor` 或 `miniterm`。传入 ELF 可启用二进制日志解码：

```sh
idf-monitor --port /dev/ttyUSB0 build/esp32s3_nanomq/zephyr/zephyr.elf
```

以下是一次实际启动的日志片段（略去部分 DEBUG 行与路径前缀，SSID 以占位符替换）：

```
I (octal_psram): vendor id    : 0x0d (AP)
I (octal_psram): density      : 0x05 (128 Mbit)
I (esp_psram): Found 16MB PSRAM device
I (esp_psram): Speed: 80MHz
I (esp_psram): SPI SRAM memory test OK
*** Booting Zephyr OS build v4.4.0-4779-g11a87708d415 ***
wifi: connecting to "<your-ssid>" (attempt 1)
wifi: connected to "<your-ssid>"
wifi: connected — starting DHCPv4 client
wifi: IPv4 address assigned (DHCPv4)
sntp: ntp.aliyun.com: epoch=1789110288, realtime seeded
net: iface 0x3fc96fb0 dev=wifi up=1
net: ipv4 192.168.1.10
2026-09-11 07:04:49 [0] DEBUG broker.c:1033 broker: listener init finished
2026-09-11 07:04:49 [0] DEBUG broker.c:1050 broker: HTTP init finished
2026-09-11 07:04:49 [0] INFO  web_server.c:560 start_rest_server: http://0.0.0.0:8081/api/v4
2026-09-11 07:04:49 [0] WARN  broker.c:1323 broker: NanoMQ (ver 0.25.1) Serving HTTP Server on http://(null):8081
NanoMQ Broker is started successfully!
```

启动是一条顺序链路：PSRAM 检测与内存自检 → Zephyr 启动 → Wi-Fi 关联 → DHCP 获取地址 → SNTP 播种真实时钟 → broker 初始化完成。就绪判据是最后一行 `NanoMQ Broker is started successfully!`。

### 5. 验证连接

确认 broker 就绪后，从同一网段的主机发起验证。REST 接口：

```sh
curl -u admin:public http://<board-ip>:8081/api/v4/brokers/
```

MQTT 收发（使用 mosquitto 客户端，任一终端订阅）：

```sh
mosquitto_sub -h <board-ip> -t 'test/#' -v
```

另一终端发布：

```sh
mosquitto_pub -h <board-ip> -t 'test/hello' -m 'hello from mosquitto' -q 1
```

订阅端应收到 `test/hello hello from mosquitto`。

### 关于开发板

本 demo 在 [ESP32-S3-LCD-EV-Board](https://docs.espressif.com/projects/esp-dev-kits/zh_CN/latest/esp32s3/esp32-s3-lcd-ev-board/user_guide.html#esp32-s3-lcd-ev-board-v1-5) 上验证，使用 ESP32-S3-WROOM-1-**N16R16V** 模组（16 MB flash + 16 MB **八线** PSRAM）。板级配置使用 SoC 相同的 `esp32s3_devkitc/esp32s3/procpu`，模组差异由 `boards/esp32s3_devkitc_procpu.overlay` 表达。**若所用模组不是 N16R16V，需要修改该 overlay**——八线与四线 PSRAM 并非修改参数即可互换。

资源占用（链接报告）：FLASH 约 958 KB，内部 SRAM 约 310 KB / 399 KB，PSRAM 窗口约 5.2 MB。

需要说明「PSRAM 窗口」的含义：模组虽有 16 MB PSRAM，但链接脚本只映射并征用其中一部分——包含 `CONFIG_ESP_SPIRAM_HEAP_SIZE` 指定的 4 MB broker 堆，以及从内部 SRAM 迁出的静态池（Wi-Fi 驱动 `.noinit`、net_buf 池、Zephyr POSIX 对象池等）。5.2 MB 是这部分被征用的映射区域，不是 PSRAM 的容量。

关于时钟：开发板无 RTC，因此 DHCP 绑定完成后 `main.c` 会查询一次公共 SNTP 服务器以播种 `CLOCK_REALTIME`，日志时间戳由此为真实 UTC。该步骤是尽力而为的：若无服务器应答，broker 仍会正常启动，仅时间戳停留在 1970 纪元（在当前 SNTP 超时配置下，最坏情况会额外增加约 9 秒启动延迟）。

## 编译与运行：qemu_x86

### 1. 编译

应用 e1000 补丁后：

```sh
west build -b qemu_x86 -d build/zephyr_broker demo/zephyr_broker
```

构建成功后的输出：

```
[288/289] Building C object zephyr/CMakeFiles/zephyr_final.dir/misc/empty_file.c.obj
[289/289] Linking C executable zephyr/zephyr.elf
Memory region         Used Size  Region Size  %age Used
             RAM:     2534228 B        31 MB      7.80%
        IDT_LIST:           0 B         2 KB      0.00%
Generating files from build/zephyr_broker/zephyr/zephyr.elf for board: qemu_x86/atom
```

### 2. 运行

```sh
west build -d build/zephyr_broker -t run
```

端口转发配置位于 `prj.conf` 的 `CONFIG_NET_QEMU_USER_EXTRA_ARGS`。如需自行控制（例如将日志写入文件、或更换宿主端口），可手动启动：

```sh
qemu-system-i386 -m 32 -cpu qemu32,+nx,+pae,sse,sse2,pni -machine q35 \
    -device isa-debug-exit,iobase=0xf4,iosize=0x04 -no-reboot -machine acpi=off \
    -serial file:/tmp/qemu.log -display none \
    -netdev user,id=n1,hostfwd=tcp:127.0.0.1:1883-:1883,hostfwd=tcp:127.0.0.1:8081-:8081,hostfwd=tcp:127.0.0.1:8083-:8083 \
    -device e1000,netdev=n1 \
    -kernel build/zephyr_broker/zephyr/zephyr.elf
```

其中 `qemu-system-i386` 并不由 Zephyr 自动提供：`west build -t run` 会从 Zephyr SDK 的 hosttools 中定位它，若手工执行则需保证该二进制在 `PATH` 中（发行版包，如 Fedora 的 `qemu-system-x86`，同样可用）。若提示 `command not found`，请改用上面的 `-t run`，或将 SDK hosttools 目录加入 `PATH`。

这里的 `-m 32` 是为 QEMU 分配 32 MB 内存；Zephyr 的链接脚本从中保留了一部分，因此链接报告中的可用 RAM 约为 31 MB（本文镜像占用约 2.4 MB，其中约 1 MB 为 malloc arena）。

### 3. 启动日志

以下为实际启动输出（省略了部分 DEBUG 行与路径前缀）：

```
*** Booting Zephyr OS build v4.4.0-4779-g11a87708d415 ***
rtc: CMOS clock 2026-09-11 05:38:46 UTC, realtime seeded
net: iface 0x19c94c dev=eth0 up=1
net: ipv4 10.0.2.15
2026-09-11 05:38:46 [0] DEBUG broker.c:1024 broker: db init finished
2026-09-11 05:38:46 [0] DEBUG broker.c:1033 broker: listener init finished
2026-09-11 05:38:46 [0] DEBUG broker.c:1050 broker: HTTP init finished
2026-09-11 05:38:46 [0] INFO  web_server.c:560 start_rest_server: http://0.0.0.0:8081/api/v4
2026-09-11 05:38:46 [0] WARN  broker.c:1323 broker: NanoMQ (ver 0.25.1) Serving HTTP Server on http://(null):8081
NanoMQ Broker is started successfully!
```

就绪判据是最后一行 `NanoMQ Broker is started successfully!`，随后可验证 REST：

```sh
curl -u admin:public http://127.0.0.1:8081/api/v4/brokers/
```

### 关于 malloc arena

`qemu_x86` 启用了 MMU，Zephyr 的 libc `malloc` 默认 arena 仅 16 KB，而 nng 的 Zephyr 平台分配器即为 `malloc()`，单条 pipe 的接收队列增长（`msq_len * 8` 字节）即可超出该值。`prj.conf` 将 arena 提升至 1 MB，因此镜像中约 1 MB 属于该 arena，并非 broker 自身开销。

## 运行功能测试

仓库提供了一套功能测试，覆盖两个 demo 的同一批用例。运行前需要以下依赖（runner 会检查，缺失时直接报错退出）：

```sh
python3 -m pip install paho-mqtt requests
```

系统还需提供 mosquitto 命令行客户端（`mosquitto_pub` / `mosquitto_sub`），Debian/Ubuntu 上为 `mosquitto-clients`，Fedora 上为 `mosquitto`。

`--list` 可查看全部 9 个测试组：

```sh
python3 demo/zephyr_broker/function_test.py --list
```

针对实机运行（`--no-manage` 表示 broker 已在运行，不由 runner 管理）：

```sh
python3 demo/zephyr_broker/function_test.py --no-manage --addr <board-ip> --webhook
```

`--webhook` 表示被测 broker 编译进了 webhook 转发器（实机需先在 `local.conf` 里配置，见前文）；`webhook_smoke` 组会自行启动接收器并断言事件到达。不加该参数时该组报 **SKIP** 而不是失败——因为 broker 侧无法被查询（REST 的 `/configuration/webhook` 路由没有实现，而启动横幅 `Hook service started` 是 DEBUG 级日志，实机构建不输出）。

`webhook_smoke` 组会**自己占用 18080 端口**并在结束时关掉接收器，因此运行前请先停掉任何遗留的接收器——否则它会以「端口已被占用」直接报错（broker 侧的目标 URL 是编译期固定的，端口换不掉）。想要手工观察事件，可在两组之间自行启动接收器：

```sh
python3 demo/zephyr_broker/hook_receiver.py --port 18080 --out /tmp/webhook.log
```

转发是 fire-and-forget、**不重试**：接收器未就绪时发出的那条事件就此丢失。默认配置下 webhook 是关闭的，所以正常跑整套时不会有任何 POST；只有当你为了验证 webhook 而打开了它、又没让接收器在线时，才会在 broker 日志里看到反复的 `webhook_inproc.c ... HTTP aio result error : Connection refused`——这是预期行为，不影响 broker 运行，但如前所述会拖慢计时敏感的测试组。

也可只运行指定测试组：

```sh
python3 demo/zephyr_broker/function_test.py --no-manage --addr <board-ip> \
    --group mqtt_v311 --group rest_get
```

以下为针对 ESP32-S3 实机的实际运行输出（省略容器标识与路径前缀）：

```
========================================================================
Zephyr broker functional test suite — broker 192.168.1.10:1883
groups: mqtt_v311, mqtt_v5, rest_get, ws_v311, ws_v5, webhook_smoke, capacity, ws_abort, survival
========================================================================
--no-manage: assuming a broker is already running
broker connect RTT 14.9 ms -> time-scale 4.0 (auto), retry 2 (auto)
[1/9] mqtt_v311      ...
[1/9] mqtt_v311      PASS  (64.1s)
[2/9] mqtt_v5        ...
[2/9] mqtt_v5        PASS  (108.1s)
[3/9] rest_get       ...
[3/9] rest_get       PASS  (3.3s)
[4/9] ws_v311        ...
[4/9] ws_v311        PASS  (268.8s)
[5/9] ws_v5          ...
[5/9] ws_v5          PASS  (15.9s)
[6/9] webhook_smoke  ...
[6/9] webhook_smoke  SKIP  (0.0s)
[7/9] capacity       ...
[7/9] capacity       PASS  (14.7s)
[8/9] ws_abort       ...
[8/9] ws_abort       PASS  (26.4s)
[9/9] survival       ...
[9/9] survival       PASS  (40.7s)
------------------------------------------------------------------------
RESULT: pass=8 fail=0 skip=1
```

各测试组的覆盖范围与实机耗时：

| 测试组 | 覆盖范围 | 实机耗时 |
|---|---|---|
| `mqtt_v311` | 会话、保留消息、v4/v5 互通 | 64.1s |
| `mqtt_v5` | 会话过期、用户属性、`$share`、主题别名 | 108.1s |
| `rest_get` | REST GET 全部路由 | 3.3s |
| `ws_v311` | MQTT 3.1.1 over WebSocket | 268.8s |
| `ws_v5` | MQTT 5 over WebSocket | 15.9s |
| `webhook_smoke` | 接收器收到 `client_connack` + `message_publish` | 见下 |
| `capacity` | 12 个并发 CONNECT + QoS1 回环 | 14.7s |
| `ws_abort` | WebSocket 连接异常中止时的连接池稳定性 | 26.4s |
| `survival` | 上游 `attack.py` 的缩比负载/会话 churn | 40.7s |

上表是**默认配置**（webhook 关闭）下的结果，因此 `webhook_smoke` 报 SKIP 而非失败——broker 侧无法被查询（REST 的 `/configuration/webhook` 路由没有实现，而启动横幅 `Hook service started` 是 DEBUG 级日志，实机构建不输出），只能由调用方用 `--webhook` 声明。打开 webhook 后单独跑该组的结果：

```
$ python3 demo/zephyr_broker/function_test.py --no-manage --addr 192.168.1.10 \
      --webhook --group webhook_smoke
[1/1] webhook_smoke  PASS  (2.7s)
RESULT: pass=1 fail=0
```

runner 会自动调整参数：它先测量到 broker 的 TCP 往返时延（本机环回低于 1 ms，实机经 Wi-Fi 实测 14–600 ms），判定为非本机后默认切换至 `--time-scale 4 --retry 2`。前者用于拉长 CI 脚本中按本机时延设定的 sleep，后者用于应对其中两个子测试的固有竞态。

## 移植中的两个框架层陷阱

以下两个问题都属于同一类：Zephyr 的 API 行为与直觉不符，且失败时没有任何错误提示，排查成本很高。

### 一、PSRAM 堆在首个客户端连接时损坏

ESP32-S3 的内部 SRAM 约 512 KB（可用 416 KB），而 broker 的数据面需要数 MB，因此必须使用 PSRAM。Zephyr 提供了 `shared_multi_heap` 用于管理此类多堆区域，看似正合适——**但它不是线程安全的**，底层是未加锁的裸 `sys_heap`。

而 nng 会从多个线程分配内存：poller、taskq worker，以及每条连接自身的 aio。结果是**第一个客户端连接建立时堆即损坏**。

解决方案是不使用 `shared_multi_heap_alloc()`，改为在该 PSRAM 窗口上使用一个带锁的普通 `struct k_heap`。此外，Wi-Fi 驱动的 `.noinit`、net_buf 池、Zephyr POSIX 对象池（约 60 KB）等无需 SRAM 的静态池也一并迁移至 PSRAM，将内部 SRAM 留给真正需要的部分。

### 二、net_mgmt 回调掩码不是按位匹配

Wi-Fi 连接流程需要监听多个事件。直觉上会把它们或进同一个掩码，注册一个回调：

```c
/* 错误：两个事件来自不同的 layer code */
net_mgmt_init_event_callback(&cb, handler,
    NET_EVENT_WIFI_CONNECT_RESULT | NET_EVENT_IPV4_DHCP_BOUND);
net_mgmt_add_event_callback(&cb);
```

这样注册的回调**永远不会被触发**，且没有任何日志或错误码。

原因在 `mgmt_run_slist_callbacks()`（`subsys/net/ip/net_mgmt.c`）。它判断回调是否匹配某个事件时，用的是**整段 layer code 的相等比较**，而不是按位与：

```c
if (!(NET_MGMT_GET_LAYER(mgmt_event->event) ==
      NET_MGMT_GET_LAYER(cb->event_mask)) ||
    !(NET_MGMT_GET_LAYER_CODE(mgmt_event->event) ==
      NET_MGMT_GET_LAYER_CODE(cb->event_mask)) || ...
```

把两个不同 layer code 的事件或进同一个掩码后，`cb->event_mask` 中的 layer code 等于两者的按位或，既不等于 A 也不等于 B，于是任何事件都无法匹配。以这两个事件为例，Wi-Fi 事件的 layer code 为 `0x0D`、IPv4 为 `0x03`，或运算后得到 `0x0F`，与两者均不相等。

正确做法是**每个事件注册一个回调**（handler 可以复用）：

```c
/* 正确：每个事件一个回调结构体 */
net_mgmt_init_event_callback(&wifi_cb, handler, NET_EVENT_WIFI_CONNECT_RESULT);
net_mgmt_add_event_callback(&wifi_cb);

net_mgmt_init_event_callback(&dhcp_cb, handler, NET_EVENT_IPV4_DHCP_BOUND);
net_mgmt_add_event_callback(&dhcp_cb);
```

## 结语

将 broker 运行在 MCU 上的价值在于省去一台边缘网关：同一块已在现场的开发板，多运行一个 broker，即可将异构设备收敛为 MQTT，并在链路中断时于本地缓存。

**适用边界。** 结合[附录 A](#附录-a与通用版-nanomq-的差异)的清单，当前实现适合的场景是受控网络内的小规模边缘汇聚与协议转换；在选型前需要明确以下几点：

- 不含 TLS、磁盘持久化与 MQTT 侧认证，**不适合直接作为公网或不可信网络中的 broker**；本文 demo 的默认配置监听全网卡，REST 虽启用了 Basic 认证但凭据固定且无加密，见「安全前提」。
- 无文件系统，断电后缓存消息与持久会话不会保留。
- 并发能力受 Zephyr 线程配额约束；本文未将吞吐与并发作为正式 benchmark 发布，附录 A 引用的官方性能数据出自多核 POSIX 环境，不代表本 demo 的表现。
- 除 `qemu_x86` 与 ESP32-S3 外，其他板卡均未验证。

代码位于 [nanomq/NanoMQ-Zephyr](https://github.com/nanomq/NanoMQ-Zephyr)，两个 demo 均在 `demo/` 目录下，各自的 README 记录了完整的 bring-up 过程。若在其他板卡上完成验证，或遇到新的问题，欢迎在 [NanoMQ 社区](https://github.com/nanomq/nanomq/discussions)参与讨论。

## 附录 A：与通用版 NanoMQ 的差异

本移植并非 NanoMQ 在 Zephyr 上的等价替代，在若干维度上存在明确差异。了解这些边界有助于判断其是否适用于具体场景。

**基线版本。** 本移植冻结于 NanoMQ **0.25.1**，而上游 master 已推进至 0.25.6+。上游的后续改动尚未合入。

**配置来源。** 通用版 NanoMQ 从 `nanomq.conf` 文件读取配置；Zephyr 版采用**内嵌最小 conf**——由 `conf_init()` 的内置默认值加上启动代码直接设置关键字段（如监听地址），绕过配置文件解析。`conf` 结构体的语义保持不变，仅配置来源由文件换为内存构造。

**功能裁剪。** 外围功能（REST、webhook、rule 引擎、bridge 等）的策略是**全量编译 + 运行时开关**，而非编译期裁剪，以保证同一份源码在两个平台行为一致。以下能力在本版本中不可用或未经验证：

| 能力 | 状态 |
|---|---|
| TLS / QUIC / SQLite / Parquet | 不包含。NanoNNG 的 Zephyr 移植未启用（`NNG_ENABLE_TLS/QUIC/SQLITE=OFF`） |
| IPC 传输 | 不包含（`ipc_internal=false`），因此 `nanomq ctl` 管理通道不可用 |
| rule 引擎 | 嵌入式 conf 无对应开关路径，未验证 |
| 文件系统相关 | 配置、日志、持久会话均不落盘；`$SYS` 仅反映运行时状态 |

**线程模型。** Linux 版依赖完整的 POSIX 动态线程池。Zephyr 版的 nng 线程数固定（taskq=2 / poller=1 / expire=1），叠加 Zephyr 的 POSIX 线程池上限（`CONFIG_POSIX_THREAD_THREADS_MAX=16`，每线程 16 KB 栈），broker 的并发能力受此约束。

**内存分配。** nng 在 Zephyr 上的分配器即 libc `malloc()`，因此 libc 的 malloc arena 就是 broker 堆。这一点对内存预算有直接影响（见 qemu 一节的说明）。

**时间源。** 两个 demo 均使用真实 UTC，但来源不同：`qemu_x86` 从 QEMU 的 CMOS RTC 播种，ESP32-S3 实机因无 RTC 而通过 SNTP 播种。Zephyr 不带时区数据库，显示恒为 UTC。

**已剔除的模块。** broker 的 `process.c` 依赖 `fork`/`kill`/`chdir`，无法在 Zephyr 上编译，由 demo 中一个提供同名符号的 stub 替代。其调用点全部位于 daemon 与 CLI 路径，嵌入式 broker 不会触达。

**已验证的目标平台。** `qemu_x86`（32 位）与 ESP32-S3。构建系统已包含 32 位 ARM 的原子操作回退（`NNG_ZEPHYR_NO_STDATOMIC`），但**未在真实 ARM 板卡上验证**，网络驱动、中断与内存预算均需重新验证。

## 附录 B：两个 demo 的对比

| | `demo/nanomq_esp32s3_broker` | `demo/zephyr_broker` |
|---|---|---|
| 目标平台 | ESP32-S3 实机 | `qemu_x86` |
| 硬件需求 | ESP32-S3 开发板 | 无 |
| 网络 | Wi-Fi STA + DHCP | QEMU SLIRP 用户态网络 |
| 存储 | 16 MB flash + 八线 PSRAM | 31 MB 模拟 RAM |
| 额外前提 | Wi-Fi 凭据 | Zephyr e1000 驱动补丁 |
| 适用场景 | 真实评估、硬件测试 | 开发阶段的快速验证 |

两个 demo 共用同一份应用源码与同一个 NanoNNG ExternalProject 构建，差异仅在板级与网络层。开发阶段建议使用 `qemu_x86`：一轮构建加运行仅需数秒，显著快于烧录与复位。两者的完整编译与运行步骤见正文对应章节。
