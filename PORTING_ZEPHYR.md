# NanoMQ → Zephyr 移植方案与实施报告(v1)

本文件是 NanoMQ broker 移植到 Zephyr RTOS 的完整交付文档:适配接口、
改动内容、构建/测试步骤、问题解决清单与未完成功能清单。
实施基于两个仓库(见 §2),验收目标全部通过(见 §6)。

- 工作分支:nanomq `nanomq-zephyr-v1`(基于 b6f1c422,未 rebase 上游)
- NanoNNG(nng fork)子模块:`develop` @ `a5ad52ca4`(原冻结点 `fa25da9` + 2 个修复)
- 演示应用:`demo/zephyr_broker/`(本仓库)
- 目标板:qemu_x86(Zephyr 4.4 @ 11a87708d41);真实板卡适配见 §8
- 复核:§9 待办清单 2026-09 复核 —— REST/webhook(§9-4)、持久会话/离线
  (§9-6)、$SYS client_status(§9-7)、性能(§9-10)已完成验证并补记
  §7 行 10-17 与 §4 追加 commit

---

## 1. 移植目标与范围

| 项 | 决策 |
|---|---|
| 集成形态 | demo 应用 + ExternalProject 编译 NanoNNG,不做 Zephyr module 抽象 |
| 配置来源 | 无文件系统;`conf_init()` 默认值 + 代码内最小覆盖(url/ipc_internal/log level),直调 `broker(conf)`,绕开 `broker_start()` 的文件解析/daemon 流程 |
| 协议面 | MQTT v3.1.1 + v5、QoS 0/1/2、retain、will、$SYS/client 上下线事件、`nmq-tcp://` 与 `nmq-ws://` 监听 |
| 编译面 | broker 核心源全量编译;webhook/rest/rule/aws_bridge 等按 NanoMQ 惯例"全量编入、运行时由 conf 关闭";仅剔除 process.c/tests/plugin |
| 不包含(TLS/QUIC/SQLite/Parquet 等) | 见 §9 待完成清单 |

## 2. 仓库与代码布局

```
nanomq/  (工作分支 nanomq-zephyr-v1)
├── nanomq/                  broker 核心应用层(移植裁剪处)
│   ├── apps/broker.c        __ZEPHYR__ 门控:signal 安装
│   ├── nanomq.c             __ZEPHYR__ 门控:<sys/ptrace.h>
│   └── mqtt_api.c           __ZEPHYR__ 门控:log_file_init() 的 W_OK 检查
├── nng/                     NanoNNG 子模块(fork,nanolib + MQTT 协议栈)
│   ├── src/sp/protocol/mqtt/nmq_mqtt.c   修复:nano lmq in-struct free guard
│   └── src/platform/zephyr/zephyr_pollq_poll.c   修复:zvfs_poll 失败降级(§4/§7-10)
└── demo/
    ├── cmake/nanonng_external.cmake      共享 NanoNNG ExternalProject 构建
    └── zephyr_broker/                    演示应用(CMakeLists/prj.conf/main.c/stubs/
                                           mqtt_accept.py/hook_receiver.py 验收工具)
```

## 3. 移植适配接口

### 3.1 平台适配(nng 侧,已在 NanoNNG Zephyr 移植中完成,本移植消费)

nng 通过 `nni_plat_*` 接口隔离平台。Zephyr 平台实现位于 NanoNNG
`src/platform/zephyr/`,本移植直接依赖以下事实(勿改):

| 接口/事实 | 行为 | 对 broker 的含义 |
|---|---|---|
| `nni_alloc` | `zephyr_alloc.c` = 裸 `malloc()` | **libc malloc arena 就是 broker 堆**(§5.3 的 1 MB 配置由此而来;`CONFIG_HEAP_MEM_POOL_SIZE` 只服务 `k_malloc()`,对 nng 无效) |
| 时钟/睡眠/随机 | `zephyr_clock.c` 等 | keepalive 定时、重传退避可用 |
| 网络传输 | Zephyr 原生 socket(BSD 兼容层) | tcp/ws 传输走 poll 驱动;无 IPC 传输(`NNG_TRANSPORT_IPC=OFF`) |
| 文件系统 | 无 FS 分支(`zephyr_file.c` 的 no-FS stub) | `nni_plat_file_exists/size` 已上提补齐(commit `21daab5`,§4):no-FS 分支 stub(exists→false,size→`NNG_ENOTSUP`),FS 分支为 `stat()` 实现 |
| taskq/poller | 固定线程数(见 ExternalProject:`TASKQ=2/POLLER=1/EXPIRE=1`) | broker 并发受限于此,叠加 §5.6 的 pthread 池 |
| POSIX API | Zephyr `CONFIG_POSIX_API` + 动态线程池 | nng 平台与 broker 的 pthread 都来自 16 线程池(§5.6) |

### 3.2 POSIX 依赖裁剪接口(应用层,本次新增)

broker 应用层的 POSIX 残留是唯一硬阻断,统一用 `__ZEPHYR__` 预定义
门控,不引入新抽象层:

| 文件 | 原 POSIX 依赖 | 裁剪 |
|---|---|---|
| `apps/broker.c` | `signal()`/`sigaction` 安装(DEBUG/ASAN 与常规路径) | `#if !defined(__ZEPHYR__)` 整段跳过 —— Zephyr 的 ^C/quit 由 QEMU/终端通道处理,`for(;;) nng_msleep` 主循环足够 |
| `nanomq.c` | `#include <sys/ptrace.h>` | 门控;`check_trace()` 调用点仅在 CLI 路径(不达),平台无实现不报错 |
| `mqtt_api.c` | `nng_access(dir, W_OK)`(文件日志目录检查) | 门控;文件日志后端对嵌入式恒关(`LOG_TO_FILE` 不设),跳过检查无副作用 |
| `process.c`(整个编译单元剔除) | fork/kill/chdir/`<paths.h>` | 由 demo `process_stub.c` 提供 6 个 `process.h` 符号(返回 -1)。被引用点全部位于 `daemon=true`/CLI 路径,嵌入式 broker 永不触达 |

### 3.3 编译期宏契约(应用与 libnng 必须一致)

| 宏 | 作用 |
|---|---|
| `ENABLE_LOG` | nanolib `conf.c` 据此初始化 conf_log、`log_*()` 才编出实体。**必须同时**经 app 侧 `target_compile_definitions` 与 nng 侧 `-DENABLE_LOG`(nng CMake 只吃 `NNG_*` 缓存变量,普通宏须走 `CMAKE_C_FLAGS`)传入,否则 broker 日志静默失效 |
| `SUPP_NANO_LIB` | 隐藏 `nanomq.c` 的 `main()`;保留 `get_cache_argc/argv`(rest_api.c 引用) |
| `ACL_SUPP` | conf 结构含 ACL 字段(默认开)。**应用侧与 libnng 侧必须同时定义**(libnng 经 `NNG_EXTRA_CFLAGS` 传入),仅一侧定义时 `struct conf` 布局错位(§7-12) |
| `NNG_STATIC_LIB` | NNG_DECL 修饰一致 |
| `SUPP_SYSLOG` **不设** | Zephyr 无 syslog();nanolib `log.c` 有 `__ZEPHYR__` 控制台输出路径 |

## 4. 改动内容(文件级清单)

### nng 子模块(NanoNNG,commit `c66e0cb`)
`src/sp/protocol/mqtt/nmq_mqtt.c` — 真实 broker bug 修复:
`nano_nni_lmq_fini()` / `nano_nni_lmq_resize()` 无条件 `nni_free(lmq->lmq_msgs)`。
当 rlmq cap ≤ 2 或扩容 malloc 失败时,`nni_lmq_init` 把队列数组放在
**结构体内嵌的 `lmq_buf`**(`lmq_alloc == 0`),free 结构体内指针 = 堆损坏,
客户端断开即崩。修复:镜像 core/lmq.c,仅当 `lmq_alloc > 0` 才 free。

### nanomq 应用层(commit `af0efc49`)
见 §3.2 三处 `__ZEPHYR__` 门控,行为对 POSIX 零变化(门控两侧代码完全相同)。

### demo(commit `606dfcbe`)
```
demo/cmake/nanonng_external.cmake   共享构建(§5.2)
demo/zephyr_broker/CMakeLists.txt   SOURCES 镜像 + 宏契约(§3.3)
demo/zephyr_broker/Kconfig          app 级 Kconfig 壳(KCONFIG_ROOT 语义)
demo/zephyr_broker/prj.conf         资源/网络配置(§5.3)
demo/zephyr_broker/src/main.c       入口:conf 最小覆盖 → broker()
demo/zephyr_broker/src/process_stub.c      §3.2(nng 文件缺口已上提,§4)
demo/zephyr_broker/accept.sh        宿主验收脚本(§6.2)
demo/zephyr_broker/README.md        构建/运行/验收速览
```
`CMakeLists.txt` 的 SOURCES 镜像自 `nanomq/nanomq/CMakeLists.txt`,剔除
`process.c`(stub 替代)与 `tests/`,`plugin/plugin.c` 随 `NNG_ENABLE_PLUGIN=OFF`
一并去掉。

### 2026-09 复核追加(§9-4/6/7/10 验证前置)

**demo 配置/开关与宏契约(commit `7e16adc47`)**:prj.conf 补
`CONFIG_ZVFS_POLL_MAX=16`、pthread mutex/cond 池 1024、hostfwd 8081;
Kconfig 增 `BROKER_REST_API`/`BROKER_WEBHOOK`;main.c 覆盖
`qos_duration=1`(默认 10 s,keepalive/会话到期检查粒度太粗)并启用
REST(NONE_AUTH)与 webhook(inproc hook 通道,`MESSAGE_PUBLISH(test/#)` +
`CLIENT_CONNACK` 两条规则);CMakeLists 把 `ACL_SUPP` 并入
`NNG_EXTRA_CFLAGS`。各动机详见 §7 行 10-13。

**验收工具(commit `bd96660af`)**:`mqtt_accept.py` —— stdlib-only raw-socket
MQTT 3.1.1/5 客户端(`--clean/--keepalive/--expiry/--expect/--proto`),
编码 MQTT5 PUBLISH 头顺序规范(§7-14),驱动 §9-4/6/7 场景;
`hook_receiver.py` —— webhook POST 接收器(§9-4)。

**nng 子模块追加(commit `a5ad52ca4`,superproject bump `646631ce5`)**:
`src/platform/zephyr/zephyr_pollq_poll.c` — `poll()` 失败降级与 100 ms
超时轮询(§7-10)。

**nng 文件探针补齐(commits `21daab5` + `c66e0cb`,superproject bump `2e4fb162`)**:
`zephyr_file.c` 的 no-FS 与 FS 两个分支此前都未实现
`nni_plat_file_exists/size`(platform.h 声明、posix/win 均已实现)——nanolib
`file.c`(`nano_file_exists`)与 `log.c` 无条件引用,消费即缺符号(§3.1),
demo 原以 `nng_plat_stub.c` 兜底,本次上提后该文件与 CMake 条目一并删除;
core/file 增 `nni_file_exists/size` 中间层,公共 `nng.h` 暴露
`nng_file_exists`/`nng_file_size`(nng.c 封装,对齐既有 `nng_file_*` 族)。

### Zephyr 环境补丁(不在本仓库,§5.4)
`drivers/ethernet/eth_e1000.{c,priv.h}` — RCTL_BAM(上游缺失 bug)。

## 5. 编译步骤

### 5.1 环境
- Zephyr 4.x west workspace(SDK 含 qemu_x86 hosttools);Zephyr checkout **必须**含 §5.4 补丁
- 本文档开发环境:docker 容器 `zephyr-tap`,`ZephyrProject` 目录 bind-mount 到 `/workdir`;宿主 Fedora 提供 mosquitto-clients(仅验收用)
- 代码同步:`git submodule update --init nng`(锁定 `c66e0cb`)

### 5.2 构建
在容器 `zephyr-tap` 内构建(仓库 bind 于 `/workdir/nanomq`,west 顶在
`/workdir`;SDK/工具链只在容器里,宿主不可编译;`-u root` 因 bind 文件
属主是宿主 uid 1001 而容器默认用户是 1000):
```sh
docker exec -u root zephyr-tap sh -lc '
  cd /workdir/nanomq &&
  git submodule update --init nng &&
  ZEPHYR_TOOLCHAIN_VARIANT=zephyr ZEPHYR_SDK_INSTALL_DIR=/opt/toolchains/zephyr-sdk-1.0.1 \
    west build -b qemu_x86 -d /workdir/build/zephyr_broker demo/zephyr_broker'
```
普通(非容器)west workspace 同命令去掉两个环境变量即可;输出目录默认
`build/zephyr_broker/`,本环境为 `/workdir/build/zephyr_broker/`。
- NanoNNG 经 ExternalProject 编入 `build/zephyr_broker/nanonng_build/`
  (`cmake --build <dir> --target nng`),libnng.a 静态导入链接
- 架构旗标(32 位 x86):`-march=i686 -mno-sse2/-sse3/-ssse3/-movbe`
  (cmpxchg8b 提供 64 位原子;剥离 SoC 的 `-march=atom`,QEMU `qemu32` CPU 不支持 movbe,#UD)
- **坑**:子模块源改动后 `west build` 增量可能不触发 ExternalProject 重编 ——
  用 `strings zephyr.elf | grep <旧串>` 断言;必要时
  `cmake --build build/zephyr_broker/nanonng_build --target nng` 强制
- 产物:RAM 占用约 2.4 MB / 31 MB(≈1 MB 为 malloc arena)

### 5.3 prj.conf 关键项(完整见文件)
```
CONFIG_POSIX_API=y / POSIX_THREAD_THREADS_MAX=16 / DYNAMIC_THREAD_STACK_SIZE=16384
CONFIG_ETH_E1000=y                         # SLIRP 只认以太网 L2,必须有真实 NIC 驱动
CONFIG_NET_QEMU_USER=y
CONFIG_NET_QEMU_USER_EXTRA_ARGS="hostfwd=tcp:0.0.0.0:1883-:1883,hostfwd=tcp:0.0.0.0:8081-:8081,hostfwd=tcp:0.0.0.0:8083-:8083"  # 8081=REST(§9-4),8083=WS(§9-2)
CONFIG_NET_CONFIG_MY_IPV4_ADDR="10.0.2.15" # SLIRP 固定 guest 概念地址
CONFIG_COMMON_LIBC_MALLOC_ARENA_SIZE=1048576   # ★ MMU 下 malloc arena 即 broker 堆
CONFIG_ZVFS_POLL_MAX=64 / MAX_PTHREAD_MUTEX/COND_COUNT=1024     # §7-10/11/18
CONFIG_MAX_PTHREAD_RWLOCK_COUNT=256                            # §7-21(topic 树节点按把)
CONFIG_NET_MAX_CONTEXTS=32 / CONFIG_NET_MAX_CONN=32            # ★ §7-18 连接池
CONFIG_ZVFS_OPEN_ADD_SIZE_NET=32                               #   zvfs fd 表
CONFIG_BROKER_REST_API=y / CONFIG_BROKER_WEBHOOK=y / CONFIG_BROKER_WS=y   # §9-4;WS 见 §9-2
CONFIG_X86_SSE/SSE2/SSE3(SSSE3 禁)
CONFIG_BROKER_LOG_DEBUG=y                  # 调试用;正式运行可关
```

### 5.4 运行与 Zephyr 补丁
杀旧实例与启动分两条独立 `docker exec`(`[i]` 括号防 pkill 自匹配,
§7-5/8);串口日志在容器内 `/tmp/qemu3.log`:
```sh
docker exec -u root zephyr-tap pkill -f "qemu-system-[i]386" || true

docker exec -u root zephyr-tap sh -lc '
  /opt/toolchains/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/qemu-system-i386 \
    -m 32 -cpu qemu32,+nx,+pae,sse,sse2,pni -machine q35 \
    -device isa-debug-exit,iobase=0xf4,iosize=0x04 -no-reboot -machine acpi=off \
    -serial file:/tmp/qemu3.log -display none \
    -netdev user,id=n1,hostfwd=tcp:0.0.0.0:1883-:1883,hostfwd=tcp:0.0.0.0:8081-:8081,hostfwd=tcp:0.0.0.0:8083-:8083 \
    -device e1000,netdev=n1 \
    -kernel /workdir/build/zephyr_broker/zephyr/zephyr.elf &'
```
**就绪判据**(`docker exec zephyr-tap tail -f /tmp/qemu3.log`):依次出现
`rtc: CMOS clock … UTC, realtime seeded` → `net: ipv4 10.0.2.15` →
`broker: NanoMQ (ver 0.25.1) Serving HTTP Server on http://(null):8081` →
`NanoMQ Broker is started successfully!`。日志时间为**真实 UTC**(demo
main.c 启动时从 QEMU CMOS RTC 播种 CLOCK_REALTIME,2026-09 修复;
Zephyr 无 TZ 数据库,显示恒为 UTC,见 §8)。hostfwd 三端口(1883/8081/8083)与
prj.conf `CONFIG_NET_QEMU_USER_EXTRA_ARGS` 一致 —— `west build -t run`
会自动带上,手动 qemu 必须显式列出,漏 8081 则 REST(§9-4)不通,漏
8083 则 WS(§9-2)不通。
客户端落点:宿主 mosquitto/accept.sh → 容器 IP(`docker inspect -f
'{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' zephyr-tap`,
实测 172.17.0.2):1883,或经宿主 socat 转发用 127.0.0.1(命令见 demo
README);容器内 python(mqtt_accept.py/hook_receiver.py) → 127.0.0.1;
宿主 curl → 容器 IP:8081(容器内无 mosquitto/curl);宿主 paho
(`transport="websockets"`,路径 `/mqtt`)→ 容器 IP:8083。
**必需环境补丁(RCTL_BAM)**:QEMU e1000 设备模型复位后清零 RCTL(真实硬件
默认置位 BAM=bit15),Zephyr `eth_e1000` 驱动从不置 BAM → **所有广播帧
(ARP!)被模型静默丢弃**,SLIRP 永远无法完成首个 TCP 连接。
上游 Zephyr 同缺(main 2026-06 核实)。补丁 2 行:
```c
// eth_e1000_priv.h
#define RCTL_BAM    (1 << 15)
// eth_e1000.c(e1000_eth_init 的 RCTL 写)
iow32(dev, RCTL, RCTL_EN | RCTL_MPE | RCTL_BAM | DT_INST_PROP(inst, rdmts) << RDMTS_OFFSET);
```

## 6. 测试

### 6.1 单元/构建级回归
- **宿主基线构建回归**(任务 4):nng 子模块在 POSIX 宿主上编译通过 —
  保证 `__ZEPHYR__` 门控与 lmq 修复不破坏非 Zephyr 构建(nng 自身
  `NNG_TESTS` 与 NanoMQ `NANOMQ_TESTS` 在本移植保持关闭;门控代码在
  POSIX 侧逐字不变,由 #if 双侧同源码保证)。
- **插桩残留断言**:`strings zephyr.elf` 不含 `DBG PIPEFINI/LMQFINI/E1000: isr/IP4IN/TCPIN`。
- **崩溃回归对比**:修复前(16 KB arena + 无 lmq guard)首次连接断开即
  heap 损坏(`right_chunk`),≤25 次连接循环内必崩;修复后连续
  **92 次 PIPEFINI(连接/断开)零崩溃**(验收两轮另计)。

### 6.2 功能验收(宿主 mosquitto)
```sh
./demo/zephyr_broker/accept.sh [host] [port]   # 默认 127.0.0.1:1883
# 容器开发环境:accept.sh 172.17.0.2 1883
```
用例与结果(干净构建 ×2 轮,均 7/7 PASS):

| # | 用例 | 覆盖点 | 结果 |
|---|---|---|---|
| 1 | QoS0 pub/sub | 连接建立、订阅树、投递 | PASS |
| 2 | QoS1 pub/sub | PUBACK 流程 | PASS |
| 3 | QoS2 pub/sub | PUBREC/PUBREL/PUBCOMP 流程 | PASS |
| 4 | Retain | retained 消息存储、迟到订阅者投递(`--retained-only`) | PASS |
| 5 | Will | SIGKILL 异常断开 → broker 检测 → will 发布 | PASS |
| 6 | MQTT v5 pub/sub | v5 CONNECT、user-property 透传 | PASS |
| 7 | v5 response-topic + correlation-data | 请求/响应辅助属性往返 | PASS |

额外观察(诊断中确认的协议正确性):正常 DISCONNECT 清除 will(DISCONNECT
处理后 `will_flag=0`,观察者收不到 will —— 合规行为);$SYS 上下线事件
(`$SYS/brokers/client_status/<id>`)正常发布;v3 与 v5 客户端混跑无串扰。

### 6.3 验收过程中的脚本陷阱(已编码进 accept.sh 注释)
- mosquitto `-k` keepalive 最小值为 **5**;`-k 2` 直接退出,will-client
  从未连接(曾误判为 broker 不发布 will)
- `kill -9` 必须打在 mosquitto_sub 自身,而非 `timeout` 包装进程 ——
  包装进程被杀后子进程存活并正常 DISCONNECT(行为正确但测不到 will)

## 7. 问题解决清单

| # | 现象 | 根因 | 修复/规避 | 归属 |
|---|---|---|---|---|
| 1 | SLIRP→guest 连接全部失败(host 握手完成、guest 无收包) | QEMU e1000 模型复位清 RCTL_BAM;Zephyr 驱动不置 BAM → 广播 ARP 被模型丢弃 | Zephyr 补丁 2 行(§5.4);README 记录;上游修复候选 | 环境(上游 bug) |
| 2 | 客户端断开即 heap 崩溃(free→sys_heap right_chunk) | 双根因:(a) MMU 下 malloc arena 默认仅 16 KB,nng 裸 malloc,per-pipe rlmq 扩容全失败;(b) NanoMQ nano lmq fini/resize 无条件 free 内嵌 `lmq_buf` | prj.conf `COMMON_LIBC_MALLOC_ARENA_SIZE=1 MB` + nng `lmq_alloc>0` guard(§4) | 配置 + 真实 bug |
| 3 | 首次 probe 偶发假阳性 "CONNACK ok" | 诊断 poller(100 ms 循环 accept fd==3)抢走并 close 了 pending 连接 | 移除诊断插桩(git checkout 恢复平台文件) | 调试自伤 |
| 4 | ISR 插桩后 RX 永不触发 | 插桩 printk 二次读 ICR 将其清零(ICR 读即清),真实分支永远看不到位 | 先读一次到局部变量再打印;最终插桩全部移除 | 调试自伤 |
| 5 | kill 脚本把 qemu 一起杀掉(exit 143) | `pgrep -f "qemu-system-i386"` 匹配自身 bash 命令行 | `pgrep -f "qemu-system-[i]386"` + 独立命令 kill | 脚本 |
| 6 | will 用例失败 | mosquitto `-k 2` 非法(min 5),客户端从未连接 | 改 `-k 5`(§6.3) | 脚本 |
| 7 | v5r 用例"挂起" | v5r pub 无超时等待 PUBACK;当时环境偶发(单测与终验均通过,多轮 7/7) | pub 全部加 `timeout -s KILL` 包裹;脚本永不无限挂 | 脚本健壮性 |
| 8 | qemu hostfwd 端口被旧实例占用(qemu25 无网) | 旧 qemu 未杀净,hostfwd 绑定失败 | 统一 kill 流程(bracket 技巧) | 流程 |
| 9 | 容器 heredoc/python 相对路径失效、git checkout 权限拒绝 | bind mount + 容器 root/宿主 uid 差异;`docker exec` 默认不接 stdin | `docker exec -i -u root`、`-w <dir>` 显式化;宿主对 zephyr/.git 只读操作改容器内 root 执行 | 环境操作 |
| 10 | connect 后首个 MQTT 包 ~5 s 才处理,负载下间歇 stall | Zephyr `CONFIG_ZVFS_POLL_MAX` 默认 3;fd 集超限时 `zvfs_poll` 整组返回 -1/ENOMEM 且不标 POLLNVAL,nng pollq 无错误分支 → 忙等自旋,全部 socket I/O 饿死 | `CONFIG_ZVFS_POLL_MAX=16` + nng `zephyr_pollq_poll.c` `poll()` 失败降级(msleep 10)与 100 ms 超时轮询(commit `7e16adc47`/`a5ad52ca4`) | 配置 + nng 平台修复 |
| 11 | REST+webhook 同开时 boot 期线程不可见挂死 | pthread mutex/cond 固定池耗尽(nng 动态分配,REST+webhook 基线即 ~245/256);`zephyr_thread.c` 池耗尽 RETRY FOREVER | `CONFIG_MAX_PTHREAD_MUTEX_COUNT/COND_COUNT=1024` | 配置 |
| 12 | REST 开时偶发 boot 崩溃(`pthread_mutex_lock: Invalid argument` → nni_panic) | `struct conf` 布局两侧不一致:app 侧 `-DACL_SUPP`、libnng 侧未定义 → `auth_http` 字段错位,锁到垃圾 `acl_cache_mtx` | `ACL_SUPP` 并入 `NNG_EXTRA_CFLAGS`(与 ENABLE_LOG 同机制,§3.3) | 构建契约 |
| 13 | keepalive/会话到期按 10 s 粒度触发,验收不可控 | `conf_init` 默认 `qos_duration=10 s`,NanoMQ 每 qos_duration tick 做一次检查 | main.c 覆盖 `qos_duration=1`(嵌入式 demo 无配置文件) | 配置 |
| 14 | v5 QoS1 发布被 broker 断开(rc 130 Malformed Packet) | 验收客户端照抄 SUBSCRIBE 布局,把 v5 PUBLISH properties 放在包标识符前;MQTT5 PUBLISH 头顺序 = topic → pid → properties,broker 解析 pid=0 → 130 | `mqtt_accept.py` 修正(properties 移至 pid 后;broker 行为合规) | 脚本 |
| 15 | REST `clients` 查询恒空 | 返回 JSON 顶层键是 `data`(非 `clients`) | 轮询脚本取 `data` 数组 | 脚本 |
| 16 | 每 host↔guest 交换固定 ~110 ms(QoS1 PUBACK、PINGRESP 等) | Zephyr TCP delayed-ACK(`tcp.c ACK_DELAY=K_MSEC(100)`,RFC 813:无 PSH 段或小窗口时推迟 ACK ~100 ms) | 识别为栈特性非缺陷;QoS0 单向数据面不受影响(§9-10) | 环境(Zephyr 栈) |
| 17 | DEBUG 日志镜像吞吐骤降(qos0 ~150-250 msg/s) | 每包多次 DEBUG 经仿真串口,串口是吞吐瓶颈(~380 行/s) | 压测用静默镜像(临时去 `CONFIG_BROKER_LOG_DEBUG`),产线默认关 | 方法/环境 |
| 18 | 并发/连发新连接被 RST(mosquitto `Connection was lost`,CI v5 套件逐轮随机失败) | Zephyr 连接池默认过小:`NET_MAX_CONTEXTS=6`(每 socket 一个 context)+`NET_MAX_CONN=8`,两个 listener 已占 2;池尽时 `tcp_conn_new()` 的 `net_context_get()` 失败 → `net_tcp_reply_rst()`(`subsys/net/ip/tcp.c`),客户端见 RST | prj.conf 提池:`NET_MAX_CONTEXTS/NET_MAX_CONN=32`、`ZVFS_OPEN_ADD_SIZE_NET=32`、`ZVFS_POLL_MAX=64`(§9-12) | 配置 |
| 19 | 无流量时 guest CPU ~90% 自旋(webhook 接收器未启动时必现) | webhook 的 HTTP 出站拨号失败后该 pfd 未被 fini,仍以 POLLOUT(0x04)挂在 pollq;`poll()` 每轮都报 POLLERR(0x08),而 `pfd->events &= ~events` 用 0x08 清不掉 0x04 → 每轮立即返回,空转 | nng `zephyr_pollq_poll.c`:revents 含 POLLERR/POLLHUP/POLLNVAL 时置 `pfd->events = 0`(该描述符已不可用,重挂或拆除交由回调决定);复现脚本 CPU 86–94% → 1.3–3.0% | nng 平台修复 |
| 20 | WS 反复连接/断开后 broker 停摆:新连接能 CONNACK,但管道不再收发也不回收 | `nmq_websocket.c` 的 `wstran_pipe_recv_cancel` 先清空 `p->user_rxaio` 再 abort rxaio,却没完成用户 aio(完成行被注释掉);rxaio 的完成路径见 `user_rxaio` 已空即跳过完成 → 该 aio 永久挂起 → `nano_pipe_stop` 中 `nni_aio_stop(&p->aio_recv)` 阻塞全局唯一的 reap 线程 → 所有 pipe 回收停摆 | 采用上游修复 `6467b6c` + `1d8127c`(两个 cancel 路径都完成用户 aio;cb 在 `skip:`/`reset:` 先释放 `user_rxaio` 再完成;qsaio 回调不再无锁读 `user_txaio`);gdb 复核 reap 线程空闲、pollq 仅剩 3 个 listener fd | 真实 bug(上游已修) |
| 21 | 生存组压到 ~30 个主题时 guest `panic: pthread_rwlock_init: pool exhausted` → 客户端 `Connection refused` | Zephyr rwlock 为固定池(默认 32),nanolib 每个 topic 树节点取一把(`mqtt_db.c` 的 `dbtree_node_new`/`dbtree_node_free` 成对),负载测试的主题数轻易超池 | prj.conf `CONFIG_MAX_PTHREAD_RWLOCK_COUNT=256`(§5.3/§9-13);宿主 POSIX 无固定池,仅 Zephyr 需显式预算 | 配置 |

## 8. 局限与已知取舍
- 线程模型:nng 平台 poller/taskq + POSIX 动态线程池上限 16(`CONFIG_POSIX_THREAD_THREADS_MAX`),broker 连接并发受其约束;栈 16 KB/线程
- 时间戳:guest 日志已从 2026-09 起为真实 UTC —— demo main.c 启动时读
  QEMU CMOS RTC(默认 `-rtc base=utc`)播种 `CLOCK_REALTIME`(lib/os/clock.c
  的 offset+uptime 模型,播种后持续走时);Zephyr 无 TZ 数据库,显示恒为
  UTC;真实板卡需自行接 RTC/时区处理
- 无文件系统:配置/日志/持久会话均无落盘;`$SYS` 只服务运行时
- 目标板:qemu_x86(32 位)验证;同 ExternalProject 已含 32 位 ARM 原子回退
  (`NNG_ZEPHYR_NO_STDATOMIC`),但**未在真实板卡验证**(网络驱动、中断、内存
  预算均需重验)
- qemu_x86 MMU 分支 → arena 静态 1 MB;小内存目标板需重算(见 §9)

## 9. 移植功能清单与验证状态(2026-09 复核)

验证环境:qemu_x86 同一镜像(demo 已从 QEMU CMOS RTC 播种真实时钟,
日志为 UTC,§5.4)。
扩展场景由 demo 验收工具(commit `bd96660af`)驱动:`mqtt_accept.py` 在
qemu 容器内连 `127.0.0.1:1883`(SLIRP hostfwd 在容器命名空间内),
REST 走 `:8081`,webhook 接收器 `hook_receiver.py` 挂在 10.0.2.2 别名
对应的容器内。

1. **Zephyr 上游修复跟进**(待办):eth_e1000 RCTL_BAM 提 PR(上游缺失,
   2026-06 核实,§5.4)。nng no-FS stub 缺 `nni_plat_file_exists/size` 一项
   已于 2026-09 上提修复(commit `21daab5`/`c66e0cb`,§4),demo 兜底 stub
   随之删除
2. **WS 传输**(✅ 2026-09-08 端到端验证):demo 加运行时 WS 监听
   (`CONFIG_BROKER_WS` → main.c 设 `websocket.enable/url`,§5.3)+ SLIRP
   8083 hostfwd;宿主 paho(`transport="websockets"`,路径 `/mqtt`)与
   CI `ws_test.py`(v3.1.1)/`ws_v5_test.py`(v5)全绿(§9-13)。两处此前
   未暴露的缺陷随之修复:① `nng/src/sp/transport/CMakeLists.txt:32` 的
   门控写成从未定义的 `NNG_TRANSPORT_MQTT_WS`,导致 mqttws 传输在**所有**
   构建里被静默排除(demo 的 `NNG_TRANSPORT_MQTT_BROKER_WS=ON` 一直空转),
   改为 `NNG_TRANSPORT_MQTT_BROKER_WS OR ..._WSS`;② WS 管道回收停摆
   (§7-20,上游已修,采用 `6467b6c`+`1d8127c`)。TLS(`nmq-wss://`)仍不可用
   (NanoNNG Zephyr 关闭 TLS)
3. **TLS/QUIC/SQLite/Parquet**(保持关闭):NanoNNG Zephyr 移植明确未包含
   (NNG_ENABLE_TLS/QUIC/SQLITE=OFF);如需支持需先在 NanoNNG 完成
4. **HTTP/REST/Webhook**(✅ 行为已验证,2026-09;rule-engine 除外):
   - REST:`curl :8081/api/v4/clients` 返回 JSON,连接建立 ~0.4 s 后出现
     于 `data` 列表、断开 ~5.7 s 后消失(顶层键是 `data`,§7-15);
     conf 侧 `http_server.enable` + `NONE_AUTH`(conf_init 默认 BASIC)
   - webhook:host 侧 `hook_receiver.py` 收到规则 POST —— 每个连接 1 条
     `client_connack`(含 clientid/proto_ver/keepalive/conn_ack),每条
     `test/#` 发布 1 条 `message_publish`(含 ts/topic/qos/payload);
     验证了"嵌入式 conf → inproc hook 通道 → nng HTTP client → 外部
     接收器"整条转发链
   - rule-engine:嵌入式 conf 无对应开关路径,未验证(维持原状)
5. **IPC cmd server**(保持关闭):`ipc_internal=false`,NNG_TRANSPORT_IPC=OFF
   —— `nanomq ctl` 管理通道不可用;如需需引入 IPC 传输
6. **持久会话/离线消息**(✅ 4 场景全过,2026-09):
   前提:main.c `qos_duration=1`(§7-13,keepalive/会话检查按 1 s tick);
   broker keepalive backoff 默认 1.5(conf.c)
   - v3.1.1 `clean=0`:客户端离线期间 broker 日志 `msg cached`,同 ID
     clean=0 重连收到缓存 QoS1 消息(CONNACK session_present=1)
   - v5 `clean=0` + `--expiry 30`:expiry 窗口内重连同样收到离线 QoS1
   - v5 expiry 到期清理:断开后 31 s(expiry 30 + 1 tick)broker 清会话/
     订阅树($SYS 下线、缓存释放)
   - keepalive 超时:keepalive=2 的静默客户端 ~5.6 s 被踢(1.5×2 s +
     检查粒度),REST 列表消失,$SYS offline reason_code `8d`
     (141,Keep Alive timeout)
7. **$SYS 指标面**(✅ client_status 已验证;其余未逐项):外部客户端订阅
   `$SYS/brokers/client_status/#`,收到成对 JSON —— 上线事件与下线事件
   (payload 含 clientid/ts/reason_code;§9-6④ 同一次 keepalive 踢除,
   reason_code `8d`)。其余 $SYS/brokers/* 指标(消息/字节计数等)未逐项
   验收
8. **真实硬件/其他板**(待办):32 位 ARM(原子回退)、64 位板、真实
   e1000/其他网卡;内存预算:小 RAM 板需把 arena/队列/线程数重配(§8)
9. **上游对齐**(待办):nanomq-zephyr-v1 落后 origin/master 155+ commits
   (0.25.6+),发布/PR 前需 rebase 并重跑验收;NanoMQ-Zephyr 独立镜像仓库
   推送(如采用原交付决策)需同步 NanoNNG 子模块 URL
10. **性能与压力**(✅ best-effort 完成,2026-09,qemu/SLIRP):
    静默日志镜像(临时关 `CONFIG_BROKER_LOG_DEBUG`,§7-17)实测:

    | 场景 | 结果 |
    |---|---|
    | QoS0 单向,1 订阅,500 条 | ~1418 msg/s,零丢失 |
    | QoS0 单向,3 订阅,400 条 | ~1136 msg/s,零丢失 |
    | QoS1 单向,1 订阅,200 条 | ~9 msg/s(受每次交换 ~110 ms 限制) |
    | QoS1 无订阅 PUBACK / PINGREQ-PINGRESP | 中位 109.9 / 110.0 ms |

    - 数据面下推吞吐 ~1.1-1.4 k msg/s;双向/请求-应答型交互被 Zephyr
      TCP delayed-ACK 钉在 ~110 ms/次(§7-16),是栈特性而非 NanoMQ 缺陷
    - DEBUG 日志经仿真串口是主要瓶颈(§7-17);并发受 §8 线程/栈预算约束
    - SLIRP 是代理网络,qemu 数值与真实网络/板卡不可比 —— 数据面结论
      需在真实网络/板卡上复测(§8-9)
11. **CI 功能测试套件在 qemu 镜像上跑通**(✅ 2026-09-08):
    `.github/scripts/` 中适用于本 demo 的子集全绿 —— `mqtt_test.py`
    (v3.1.1)、`mqtt_test_v5.py`、`rest_api_test.py`(其余套件需 TLS/WS
    监听或宿主 nanomq 二进制,不适用)。此前 v5 套件 7 轮全败、失败点在
    会话过期/user-property/共享订阅间漂移,根因即 §7-18 连接池耗尽(池尽
    即 RST),非 broker 语义缺陷:提池 + `max_topic_alias`(见下)后
    **3/3 连续全绿**;实测同一条连接池,6 并发 CONNECT 由 2/6 变 6/6、
    50 ms 间隔连发由 9/20 变 20/20。另发现 `test_topic_alias` 依赖
    `mqtt.max_topic_alias`:`conf_init` 默认 0(CONNACK 广播
    `TOPIC_ALIAS_MAXIMUM=0`,带别名的 PUBLISH 被拒),宿主 CI conf 在
    **master** 上是 `max_topic_alias=1024`、develop 分支缺该行 —— demo
    main.c 现按 1024 对齐。运行须知:宿主 python 走系统代理会把
    `172.17.0.2:8081` 打成 502,跑 REST 套件需 `NO_PROXY=<容器 IP>`;
    本 demo 侧套件(§9-13)的 REST 组只做 GET(不碰会翻转运行配置的
    POST `/reload`)
12. **VFS 支持变体**(待定,2026-09 评估):做"可挂文件系统、进而按宿主
    方式解析 HOCON 配置/落盘"的编译开关版本。nng 平台层 `zephyr_file.c`
    已按 Zephyr 官方 Kconfig `CONFIG_FILE_SYSTEM` 分双分支(无 FS 桩:
    exists→false/size→ENOTSUP;有 FS:POSIX 风格真实实现,§4 `21daab5`
    起)—— 宏开关在 nng 层是现成架构,但开关只是必要条件,剩余门槛:
    ① 宏双侧注入(本 demo 的 libnng 是 ExternalProject 独立构建,看不到
    prj.conf 的 Kconfig,须 `NNG_EXTRA_CFLAGS -DCONFIG_FILE_SYSTEM`,守
    §3.3 宏契约);② 板级介质与挂载(storage 分区/dts + `fs_mount`,须先于
    conf 解析,qemu_x86 默认无介质);③ 路径主机假设(`/etc/nanomq.conf`、
    conf_file 推导须映射到 VFS 挂载点);④ 激活整层被桩代码(日志落盘/
    pid/license 等逐条审计);⑤ 双变体(no-FS 基线 + VFS)验证与腐烂成本。
    触发条件:真实板需要运行时改配置/持久会话落盘;此前维持内嵌最小
    conf(§1),无动作
13. **Zephyr 专用功能测试套件**(✅ 2026-09-08 8/8 全绿):
    `demo/zephyr_broker/function_test.py` —— 宿主侧 runner,自管 qemu
    生命周期(杀旧实例 → 全新日志名启动 → 轮询就绪串 → 结束回收),8 组
    各以独立子进程执行(组级崩溃/超时隔离),`--group` 单组重跑、
    `--no-manage` 复用已在跑的 broker、`--list` 列组、`-v` 显示通过组输出。
    复用 `.github/scripts/` 模块而**不改其代码**:mqtt 组覆写模块级
    `g_addr/g_port/g_url`;ws_v311 包一层 `Test.init`;ws_v5 打
    `paho.mqtt.client.Client.connect` 补丁(该脚本硬编码 `localhost:8083`)。
    组表与实测时长:

    | 组 | 驱动 | 实测 |
    |---|---|---|
    | mqtt_v311 | CI `mqtt_test.py` | 16.1 s |
    | mqtt_v5 | CI `mqtt_test_v5.py` | 28.0 s |
    | rest_get | 自写(7 条路由 + `/configuration/websocket` 镜像运行 conf) | 0.9 s |
    | ws_v311 | CI `ws_test.py`(WS 上 MQTT 3.1.1) | 209.5 s |
    | ws_v5 | CI `ws_v5_test.py`(WS 上 MQTT 5) | 6.1 s |
    | webhook_smoke | 自写(`hook_receiver.py` 收 `client_connack`/`message_publish`) | 2.8 s |
    | capacity | 自写(12 并发 CONNECT + QoS1 echo,连接池回归) | 6.2 s |
    | survival | `survival_test.py`(缩规模 `attack.py` + 压后探活) | 33.4 s |

    - 定位:CI `test.py` 在本 demo 不可用(需宿主 nanomq 二进制 + TLS/WS
      listener,并驱动 mosquitto CLI/TLS/鉴权等宿主侧工具),故按同样的
      "复用模块、注入地址"思路做 Zephyr 版
    - 抖动如实记录:ws_v5 修前首跑 11.7 s 失败,原因是树内
      `.github/scripts/ws_v5_test.py` 为旧版 —— 把 CONNECT 专属属性
      `MaximumPacketSize` 设进 PUBLISH 属性,paho 2.x 在客户端线程抛
      `MQTTException`,v5 发布端全部未能连接;换上游 master 版本(其
      `func()` 自行处理 `conn_prop`,并给等待加上界)后 6.1 s 通过。ws 组
      默认多一次重试(`--retry-ws`):上游脚本用固定 sleep 适配宿主 broker,
      SLIRP 延迟下首跑可能抖动;真实抖动不做隐藏
    - 生存组压出 §7-21(rwlock 池耗尽),修复后 33.4 s 通过;组内
      `attack.py` 常量缩规模(30 s、2 发布者、8 噪声客户端、2 节点共享
      订阅):宿主规模是崩溃放大器,SLIRP 每连接约 9 msg/s
    - 失败语义:默认跑完全部组(`--fail-fast` 可停);FAIL 组打印末 40 行
      输出 + 串口日志尾部;组失败 exit 1,结构性失败(docker/qemu/依赖
      缺失)exit 2
    - 失败路径实测(2026-09-08):① `--no-manage --addr 10.255.255.1` →
      `ERROR: no broker answering` / exit 2;② 组间容器内 pkill qemu →
      日志 `broker not answering — restarting qemu`,该组计 FAIL(exit 1),
      后续组在新 guest 上自动继续并通过(实测 `pass=2 fail=1`)
    - 完整用法与组说明见 demo README「Functional test suite」;命令速查见
      §10 第 3b 步

## 10. 复现命令速查(容器环境 `zephyr-tap`,§5.1)
```sh
# 0) 打 Zephyr 补丁(§5.4 两行,改 /workdir/zephyr 内树,须 -u root)
# 1) 构建(§5.2 全文;镜像 zephyr-build:main 已含 SDK/工具链)
docker exec -u root zephyr-tap sh -lc 'cd /workdir/nanomq && ZEPHYR_TOOLCHAIN_VARIANT=zephyr \
  ZEPHYR_SDK_INSTALL_DIR=/opt/toolchains/zephyr-sdk-1.0.1 \
  west build -b qemu_x86 -d /workdir/build/zephyr_broker demo/zephyr_broker'
# 2) 启动(先 pkill 旧实例再启动,命令全文见 §5.4);就绪判据:
docker exec zephyr-tap sh -lc 'grep -a "NanoMQ Broker is started" /tmp/qemu3.log || tail -f /tmp/qemu3.log'
# 3) 验收(宿主;期望 RESULT: pass=7 fail=0;IP 用 §5.4 的 docker inspect 结果)
./demo/zephyr_broker/accept.sh 172.17.0.2 1883
# 3b) 功能测试套件(§9-13;宿主,自管 qemu 生命周期,期望 RESULT: pass=8 fail=0)
python3 demo/zephyr_broker/function_test.py
python3 demo/zephyr_broker/function_test.py --group ws_v5 -v      # 单组重跑
python3 demo/zephyr_broker/function_test.py --no-manage --addr 172.17.0.2  # 复用已跑 broker
# 4) 扩展场景(§9-4/6/7:容器内跑 python,宿主跑 curl)
docker exec -d zephyr-tap python3 /workdir/nanomq/demo/zephyr_broker/hook_receiver.py \
    --port 18080 --out /tmp/webhook.log            # webhook 接收器(§9-4)
docker exec zephyr-tap python3 /workdir/nanomq/demo/zephyr_broker/mqtt_accept.py 127.0.0.1 1883 \
    sub --proto 5 --clean 0 --expiry 30 --topic v5/offline --qos 1   # 离线会话(§9-6②)
curl -s http://172.17.0.2:8081/api/v4/clients      # REST(§9-4;键为 data)
```
