# NanoMQ → Zephyr 移植方案与实施报告(v1)

本文件是 NanoMQ broker 移植到 Zephyr RTOS 的完整交付文档:适配接口、
改动内容、构建/测试步骤、问题解决清单与未完成功能清单。
实施基于两个仓库(见 §2),验收目标全部通过(见 §6)。

- 工作分支:nanomq `nanomq-zephyr-v1`(基于 b6f1c422,未 rebase 上游)
- NanoNNG(nng fork)子模块:`develop` @ `f4db38440`(原冻结点 `fa25da9` + 1 个修复)
- 演示应用:`demo/zephyr_broker/`(本仓库)
- 目标板:qemu_x86(Zephyr 4.4 @ 11a87708d41);真实板卡适配见 §8

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
│   └── src/sp/protocol/mqtt/nmq_mqtt.c   修复:nano lmq in-struct free guard
└── demo/
    ├── cmake/nanonng_external.cmake      共享 NanoNNG ExternalProject 构建
    └── zephyr_broker/                    演示应用(CMakeLists/prj.conf/main.c/stubs)
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
| 文件系统 | 无 FS 分支(`zephyr_file.c` 的 no-FS stub) | `nni_plat_file_exists/size` **缺失**,由 demo 的 `nng_plat_stub.c` 补齐(上游候选修复) |
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
| nng no-FS stub | `nni_plat_file_exists/size` 缺失 | demo `nng_plat_stub.c` 补齐(exists→false,size→`NNG_ENOTSUP`) |

### 3.3 编译期宏契约(应用与 libnng 必须一致)

| 宏 | 作用 |
|---|---|
| `ENABLE_LOG` | nanolib `conf.c` 据此初始化 conf_log、`log_*()` 才编出实体。**必须同时**经 app 侧 `target_compile_definitions` 与 nng 侧 `-DENABLE_LOG`(nng CMake 只吃 `NNG_*` 缓存变量,普通宏须走 `CMAKE_C_FLAGS`)传入,否则 broker 日志静默失效 |
| `SUPP_NANO_LIB` | 隐藏 `nanomq.c` 的 `main()`;保留 `get_cache_argc/argv`(rest_api.c 引用) |
| `ACL_SUPP` | conf 结构含 ACL 字段(默认开) |
| `NNG_STATIC_LIB` | NNG_DECL 修饰一致 |
| `SUPP_SYSLOG` **不设** | Zephyr 无 syslog();nanolib `log.c` 有 `__ZEPHYR__` 控制台输出路径 |

## 4. 改动内容(文件级清单)

### nng 子模块(NanoNNG,commit `f4db38440`)
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
demo/zephyr_broker/src/process_stub.c / nng_plat_stub.c   §3.2
demo/zephyr_broker/accept.sh        宿主验收脚本(§6.2)
demo/zephyr_broker/README.md        构建/运行/验收速览
```
`CMakeLists.txt` 的 SOURCES 镜像自 `nanomq/nanomq/CMakeLists.txt`,剔除
`process.c`(stub 替代)与 `tests/`,`plugin/plugin.c` 随 `NNG_ENABLE_PLUGIN=OFF`
一并去掉。

### Zephyr 环境补丁(不在本仓库,§5.4)
`drivers/ethernet/eth_e1000.{c,priv.h}` — RCTL_BAM(上游缺失 bug)。

## 5. 编译步骤

### 5.1 环境
- Zephyr 4.x west workspace(SDK 含 qemu_x86 hosttools);Zephyr checkout **必须**含 §5.4 补丁
- 本文档开发环境:docker 容器 `zephyr-tap`,`ZephyrProject` 目录 bind-mount 到 `/workdir`;宿主 Fedora 提供 mosquitto-clients(仅验收用)
- 代码同步:`git submodule update --init nng`(锁定 `f4db38440`)

### 5.2 构建
```sh
west build -b qemu_x86 demo/zephyr_broker   # 输出 build/zephyr_broker/
```
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
CONFIG_NET_QEMU_USER_EXTRA_ARGS="hostfwd=tcp:0.0.0.0:1883-:1883"
CONFIG_NET_CONFIG_MY_IPV4_ADDR="10.0.2.15" # SLIRP 固定 guest 概念地址
CONFIG_COMMON_LIBC_MALLOC_ARENA_SIZE=1048576   # ★ MMU 下 malloc arena 即 broker 堆
CONFIG_X86_SSE/SSE2/SSE3(SSSE3 禁)
CONFIG_BROKER_LOG_DEBUG=y                  # 调试用;正式运行可关
```

### 5.4 运行与 Zephyr 补丁
```sh
qemu-system-i386 -m 32 -cpu qemu32,+nx,+pae,sse,sse2,pni -machine q35 \
  -no-reboot -machine acpi=off \
  -netdev user,id=n1,hostfwd=tcp:0.0.0.0:1883-:1883 -device e1000,netdev=n1 \
  -kernel build/zephyr_broker/zephyr/zephyr.elf
```
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

## 8. 局限与已知取舍
- 线程模型:nng 平台 poller/taskq + POSIX 动态线程池上限 16(`CONFIG_POSIX_THREAD_THREADS_MAX`),broker 连接并发受其约束;栈 16 KB/线程
- 时间戳:guest 日志 `1970-01-01 00:00:xx`(Zephyr 无 RTC),只可相对参考
- 无文件系统:配置/日志/持久会话均无落盘;`$SYS` 只服务运行时
- 目标板:qemu_x86(32 位)验证;同 ExternalProject 已含 32 位 ARM 原子回退
  (`NNG_ZEPHYR_NO_STDATOMIC`),但**未在真实板卡验证**(网络驱动、中断、内存
  预算均需重验)
- qemu_x86 MMU 分支 → arena 静态 1 MB;小内存目标板需重算(见 §9)

## 9. 待完成移植的功能清单
1. **Zephyr 上游修复跟进**:eth_e1000 RCTL_BAM 提 PR(上游缺失,2026-06 核实);
   另 nng no-FS stub 缺 `nni_plat_file_exists/size` 可上提 NanoNNG
2. **WS 传输实测**:`nmq-ws://` 已编入(`NNG_TRANSPORT_MQTT_BROKER_WS=ON`),
   未做端到端用例(需宿主 ws 客户端)
3. **TLS/QUIC/SQLite/Parquet**:NanoNNG Zephyr 移植明确未包含(NNG_ENABLE_TLS/
   QUIC/SQLITE=OFF)—— 保持关闭即可,如需支持需先在 NanoNNG 完成
4. **HTTP/REST/Webhook/Rule-Engine**:全量编入但运行时由 conf 默认关闭,
   未做任何行为验证(嵌入式 conf 无对应开关路径测试)
5. **IPC cmd server**:`ipc_internal=false`,NNG_TRANSPORT_IPC=OFF ——
   `nanomq ctl` 管理通道不可用;如需需引入 IPC 传输
6. **持久会话/离线消息**:clean-session 缓存代码在(cached_sessions、
   session_expiry),无专门用例;keepalive 检测逻辑已见日志但未按超时验证
7. **$SYS 指标面**:client_status 事件已观察;broker metrics/$SYS 全量未验收
8. **真实硬件/其他板**:32 位 ARM(原子回退)、64 位板、真实 e1000/其他网卡;
   内存预算:小 RAM 板需把 arena/队列/线程数重配(§8)
9. **上游对齐**:nanomq-zephyr-v1 落后 origin/master 155 commits(0.25.6+),
   发布/PR 前需 rebase 并重跑验收;NanoMQ-Zephyr 独立镜像仓库推送(如采用
   原交付决策)需同步 NanoNNG 子模块 URL
10. **性能与压力**:未做吞吐/并发压测;SLIRP 后端本身是代理,数据面结论
    需在真实网络/板卡上复测

## 10. 复现命令速查
```sh
# 1) 打 Zephyr 补丁(§5.4,2 行)  # 2) 构建
west build -b qemu_x86 demo/zephyr_broker
# 3) 运行(qemu 命令见 §5.4)      # 4) 验收
./demo/zephyr_broker/accept.sh 127.0.0.1 1883     # 期望 RESULT: pass=7 fail=0
```
