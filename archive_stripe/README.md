# 采集档案双冗余条带（archive_stripe）

把一份**原始采集字节**（任意二进制，不解释为文本）编码成 `k` 个数据分片加
`P`、`Q` 两个校验分片，放在同一档案目录里。任意**两片**整片丢失（无论两片
数据、两片校验，还是一数据一校验）都能从剩余 `k` 片还原原文件；三片不可用
则明确拒绝。

这是面向原始字节的容灾存储能力：不涉及观测数值计算，不读取仪器登记表，也不
调用任何纠删码库或外部存储服务。纯 Python 标准库实现（Python 3.11+）。

现有 `handover_join/`（评分/交接连接）与 `verif_pipeline/`（遥测、布点、左连接
等入口）保持原样，本目录是独立新增项。

## 快速开始

```bash
cd archive_stripe

# 打包（k 必须在 2..8，B 为块长）
python3 -B -m archive_stripe.cli pack capture.bin pack -k 4 -B 65536

# 查看健康状态（全部正常时退出码 0；已不可恢复时退出码 2）
python3 -B -m archive_stripe.cli check pack

# 还原到调用者指定的新文件（已存在的目标绝不被覆盖）
python3 -B -m archive_stripe.cli restore pack --output restored.bin
```

`check` 与 `restore` 都支持 `--json` 输出机器可读结果。失败一律返回非零退出
码（当前为 2），错误写 stderr，不会用成功码掩盖。

破坏演示（每一步都在新的子进程里调用 CLI）：

```bash
python3 -I scripts/demo.py --work-dir /tmp/archive_stripe_demo
```

演示内容：先 `check` 得到 `HEALTHY`；截短一片数据分片、翻转一片校验分片的一
个 bit，`check` 分别报 `truncated` / `rewritten`，`restore` 把两者都当作整片
擦除后仍还原出与源文件逐字节相同的内容；再破坏第三片，`check` 报
`UNRECOVERABLE`、`restore` 退出码非零，并且已存在的目标文件保持原样；最后删
掉清单，演示拒绝还原且不创建输出文件。

## 目录与格式

一个档案目录恰好包含 `k + 2` 个分片文件和一个清单：

```
<pack>/
  manifest.json            # 唯一元数据文件，纯 ASCII JSON，很小
  D0.bin ... D{k-1}.bin    # 数据分片
  P.bin                    # P = Σ D_i
  Q.bin                    # Q = Σ c_i·D_i
```

命名固定，清单里重复列出这些名字，读取方可用它确认目录确实是它声称的档案。
清单**不含源文件字节**，但足以让另一个进程只凭档案目录还原文件。

### 条带布局

源文件按 `B` 字节切块。第 `s` 个条带步（stripe step）取接下来的 `k` 个源块，
按顺序放到 `k` 个数据分片上：

```
step s:  D0 <- 源块 s*k + 0
         D1 <- 源块 s*k + 1
         ...
         D(k-1) <- 源块 s*k + (k-1)
```

随后为该步写入一个 P 块和一个 Q 块。每个数据分片每步写一个块，因此**所有分片
文件等长**：

```
stripe_length = ceil(source_length / (k*B)) * B      （空文件为 0）
```

分片文件里含多个条带步；实现不会为每条带创建新文件。源文件末尾不足一个条带
步时，缺失的块以零补齐——**补零只用于编码**。清单记录真实
`source_length`，`restore` 严格裁回该长度；空文件的清单同样可读
（`source_length=0`、`stripe_length=0`、各分片长度为 0）。

### 清单字段

```json
{
  "format": "archive-stripe",
  "version": 1,
  "k": 4,
  "block_size": 65536,
  "source_length": 1234567,
  "source_sha256": "<64 hex>",
  "stripe_length": 1245184,
  "stripes": [
    {"name": "D0.bin", "role": "data",   "index": 0, "length": 1245184, "sha256": "<64 hex>"},
    {"name": "D1.bin", "role": "data",   "index": 1, "length": 1245184, "sha256": "<64 hex>"},
    {"name": "D2.bin", "role": "data",   "index": 2, "length": 1245184, "sha256": "<64 hex>"},
    {"name": "D3.bin", "role": "data",   "index": 3, "length": 1245184, "sha256": "<64 hex>"},
    {"name": "P.bin",  "role": "parity", "index": "P", "length": 1245184, "sha256": "<64 hex>"},
    {"name": "Q.bin",  "role": "parity", "index": "Q", "length": 1245184, "sha256": "<64 hex>"}
  ]
}
```

清单说明格式版本、块长、分片位置（名字/角色/序号）、真实长度，以及每片的预期
长度与 SHA-256。读取时逐项校验：

* `format` 必须是 `archive-stripe`，`version` 必须是 1；
* `k` 在 2..8，`block_size` 在 `[1, 2^30]`；
* `source_length >= 0`，且 `stripe_length` 必须等于
  `ceil(source_length/(k*B))*B`——两者矛盾即拒绝，不猜布局；
* 分片条目个数、顺序、名字、角色、序号必须与 `k` 完全一致；
* 每片 `length` 必须等于 `stripe_length`，`sha256` 必须是 64 位小写十六进制。

清单缺失、不可读、非 JSON、字段缺失/类型错误、字段自相矛盾、`k` 或 `B` 越界，
都在写任何输出之前报错。

## 双校验算法

所有运算在 GF(256) 上，本原多项式 `0x11d`（`x^8+x^4+x^3+x^2+1`）：

* 加法/减法：逐字节异或；
* 乘法：无进位多项式乘法后对 `0x11d` 取模；
* `c_i = 2^i`（`i` 从 0 开始），即 `1, 2, 4, 8, 16, 32, 64, 128`。

每个条带步对 `k` 个数据块 `D_i` 计算：

```
P = Σ_i D_i
Q = Σ_i c_i · D_i
```

`2` 是生成元，`c_i` 两两不同，因此两个未知块的 2×2 方程组可逆。

### 恢复情形

设不可信集合为 `U`（由 `check` 逐片核验长度与摘要得出）：

| 情形 | 用的方程 | 做法 |
|---|---|---|
| `U` 为空 | 无需恢复 | 数据分片直接按布局拼回 |
| 丢 1 片数据 | `P` 或 `Q` | `D_a = P ⊕ Σ_{i≠a}D_i`；无 P 时 `D_a = (Q ⊕ Σ_{i≠a}c_iD_i)/c_a` |
| 丢 2 片数据 | `P` 和 `Q` | 解 `[1 1; c_a c_b]`，`D_a=(c_b·u⊕v)/det`，`D_b=(c_a·u⊕v)/det`，`u=P⊕Σ_known D_i`，`v=Q⊕Σ_known c_iD_i` |
| 丢 1 数据 + 1 校验 | 剩下的那个校验 | 归入“丢 1 片数据” |
| 丢 2 片校验 | 无需恢复 | 数据分片齐全，直接拼回（P/Q 不参与输出） |
| `U` 中 ≥ 3 片，或需解 2 未知但缺 P/Q | — | 明确拒绝 |

`k=8` 时两个未知片的求解与 `k=2` 是同一公式，系数取自
`c=(1,2,4,8,16,32,64,128)`；测试遍历了 `k=8` 的全部 `C(10,2)=45` 种两片丢失
组合。

**双冗余不是无条件纠错。** 它只能处理**已定位的整片擦除**：必须先知道哪片
不可信。这里的定位来自清单里的每片长度和 SHA-256。单片内部少量 bit 翻转可以
被摘要发现并整片丢弃；但如果损坏发生在多个分片上且没有可信摘要（例如清单本
身也被改），就没有足够的信息定位错误，实现会拒绝而不是拿坏字节参加方程后输
出错误文件。还原末尾还会用清单里的 `source_sha256` 对完整输出做最终校验，
不匹配则拒绝发布。

## `check` 的判定

逐片核对清单与真实文件，状态为：

| 状态 | 含义 | 可信 |
|---|---|---|
| `ok` | 存在、长度精确、SHA-256 精确 | 是 |
| `missing` | 文件不存在 | 否 |
| `truncated` | 实际长度小于清单 | 否 |
| `length_mismatch` | 实际长度大于清单（也含其他不等） | 否 |
| `rewritten` | 长度相同但 SHA-256 不同 | 否 |
| `unreadable` / `not_regular` | 无法作为普通文件打开 | 否 |

只有 `ok` 参与恢复。报告给出 `healthy`（全部 6 个分片正常）、`recoverable`、
`trusted`、`lost`、缺失的数据片与恢复模式（`identity` / `single_p` /
`single_q` / `double`）。清单本身无效时，`check` 返回 UNKNOWN 而不是猜一个
健康状态。`check` 退出码在不可恢复时为 2——健康检查不应把坏档案报告成成功。

## `restore` 的安全边界

1. 先完整校验清单；缺失/矛盾即停。
2. 逐片核验长度与摘要，**不可信分片按整片擦除**，损坏字节绝不进入方程。
3. 可信分片少于 `k`，或超过两片不可用，或需解两未知却缺校验片时，在创建任何
   输出前拒绝。
4. 重建写入目标目录内的临时文件，按清单真实长度裁剪，核对源 SHA-256，全部通
   过后才 `os.replace` 到调用者给出的路径。
5. 已存在的目标文件永不覆盖；任何失败都保留原目标（或保持“不存在”），临时文
   件被清理。

因此“失败的恢复覆盖已有目标”与“用坏字节输出错误文件”都被代码路径排除，而
不是靠文档约定。

## 内存与 I/O

编码与恢复都按块推进：

* 编码时每个条带步只持有 `k` 个块、一个 P 块、一个 Q 块，即 `O(k·B)`；
* 恢复时同样每步每片最多一个块，两未知求解只用块大小的临时缓冲；
* 所有读写使用固定大小缓冲，源文件总长度不进入内存。

实测（见 `stress_results/`）：

| 数据集 | k | B | 丢失 | 峰值 RSS | 预算 | 跑法 |
|---|---|---|---|---|---|---|
| 256 MiB | 6 | 64 KiB | 无 | 22.91 MiB | 64 MiB | `stress_demo.py` |
| 512 MiB | 8 | 256 KiB | D0+D1 | 27.18 MiB | 96 MiB | `memory_probe.py` |
| 1024 MiB | 8 | 256 KiB | D0+D1 | 27.25 MiB | 96 MiB | `memory_probe.py` |

峰值 RSS 由独立子进程的 `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` 测量；
原文件与恢复文件各自流式 SHA-256 独立核对。数据量增长 4 倍，峰值几乎不变。

大档案验证（普通回归不生成大文件，按需运行）：

```bash
python3 -I scripts/memory_probe.py --size-mib 512 --k 8 --block 262144 \
  --budget-mib 96 --work-dir /tmp/archive_mem \
  --report stress_results/memory_probe_512mib_k8.json

python3 -I scripts/stress_demo.py --size-mib 256 --k 6 --block 65536 \
  --rss-limit-mib 64 --work-dir /tmp/archive_stress \
  --report stress_results/stress_256mib_k6.json
```

## 测试

```bash
bash scripts/run_tests.sh          # python3 -B -m unittest discover -s tests -v
python3 -I scripts/run_tests.py    # 同一套用例，末尾打印摘要
```

36 个用例（普通回归约 4 秒），全部使用小样本。独立 oracle
`tests/reference_decoder.py` 不导入生产的 GF/编解码/恢复代码：它用普通
`json` 解析清单、用自己写的无进位乘法重新打包并解码，逐字节比对。

覆盖：

* **手算例**：4 字节 `00 01 02 03`、`k=2`、`B=1`，手算
  `D0=0002, D1=0103, P=0101, Q=0204`，并验证单丢、双数据丢、双校验丢的
  恢复；`examples/handcalc_pq.py` 可独立运行。
* **全组合**：`k=2,3,8` 的所有一片、两片不可用组合，逐一比较恢复文件长度与
  原始 SHA-256，并用参考解码器再算一遍。样本含空文件、全零字节、非 UTF-8 字
  节、恰好整条带、整条带+1 个尾字节、末步仅 1 字节。`k=8` 另跑全部 45 种两
  片丢失。
* **实际破坏**：截短一片并翻转另一片，`check` 分别识别为 `truncated` /
  `rewritten`，仍能精确还原；第三片损坏则拒绝且保住已有目标。单独测试每片的
  截短与翻转。
* **清单失败**：缺失清单、块长与分片尺寸不符、`source_length` 被改、
  `k=9`、字段缺失、`k=0/1/9/100`、`B=0/-1`，均拒绝且不写输出。另有一个
  “几何仍自洽但摘要对不上”的清单：重建照做、末尾摘要不符，仍拒绝发布且保住
  目标。
* **档案不被改动**：还原前后对档案目录逐文件取摘要，确认源档案保持可供排查。
* **空文件**：清单可读、6 个零长度分片、任意两片丢失仍还原为空文件。
* **打包安全**：拒绝非空目录；`--force` 原子替换且不残留 staging。
* **CLI**：`pack/check/restore` 往返；不可恢复时 `check` 与 `restore` 均退出
  码 2 且不创建输出；重复 restore 到同一目标被拒。

## 目录

```
archive_stripe/
  archive_stripe/
    gf256.py      # GF(256) 查表、0x11d、c_i=2^i、块级 xor/scale
    manifest.py   # 清单格式、原子写、严格校验（不猜布局）
    codec.py      # 按条带步编码 / 解码，P/Q 与 2×2 求解
    check.py      # 分片状态分类、健康报告、原子安装档案
    restore.py    # “先核验后恢复”，临时文件 + 摘要 + 原子发布
    cli.py        # pack / check / restore 三个命令
  tests/
    test_archive_stripe.py   # unittest 回归
    reference_decoder.py     # 独立的参考编码/解码 oracle
  scripts/
    demo.py        # 打包→检查→破坏→还原→拒绝 的独立进程演示
    memory_probe.py# 大档案 + 双片擦除的峰值 RSS 实测
    stress_demo.py # 大档案往返与 RSS 上限检查
    run_tests.sh / run_tests.py
  examples/
    handcalc_pq.py # 手算 P/Q 与四种恢复情形（不导入生产代码）
  stress_results/  # 本机实测 JSON 报告
```

## 明确的限制

* `k` 只能是 2..8；`B` 至少 1，且不超过 2^30。
* 最多两片整片擦除；三片及以上明确拒绝，不会尝试部分恢复。
* 恢复需要**完整**的清单；清单丢失时数据分片仍在，但布局不被猜测。
* 只做字节级存储冗余；不做文本/数值解释，不做压缩、加密或跨机器复制。
* 单进程、单目录；不提供后台服务或并发写入同一档案的协调。
