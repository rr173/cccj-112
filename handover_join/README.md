# 观测交接包外存左连接（handover_join）

把**事实流水**（左表）与**仪器登记资料**（右表）按配置中的复合键做左连接，
用于离线整理观测交接包。登记表不保证键唯一：同一台仪器可能有好几条待核实的
登记，因此连接**保留全部 m×n 配对**，不挑选、不推断设备身份、不按时间取最近。

输入规模可以超过内存：全部排序、归并与配对都在临时磁盘上完成，不使用数据库、
DataFrame 库或系统 `sort` 替代连接执行。纯 Python 标准库实现（Python 3.11+），
无第三方依赖。

## 输入与连接语义

两边均为逐行 JSON 对象（JSONL，UTF-8）。配置文件示例
（`examples/handover/config.json`）：

```json
{
  "left_keys":  ["instrument_id", "batch_no"],
  "right_keys": ["reg_instrument", "reg_batch"],
  "key_types":  ["string", "integer"]
}
```

* 三个数组等长、按位置对应，支持左右字段名不同；分量类型只能是 `string` 或
  `integer`，可混合。
* **整数**接受任意精度 JSON 整数（Python `int` 全程保真）；布尔值不是整数，
  `true/false` 出现在整数分量上报错。
* **字符串**按原文判等：不去空格、不改大小写；空字符串是合法键。
* 键字段缺失或值为 `null` 一律**不匹配**，包括两边同时为空的情形（这不是
  错误，只是不产生配对）。
* 非空但类型不符是错误：报错明确指出**哪一边、第几行、哪个字段、期望类型**。
* 复合键**逐分量编码比较**，绝不用分隔符拼接。业务编号自带 `|`、`,` 等字符
  不会串键（测试 `test_delimiter_strings_and_verbatim_equality` 专门覆盖）。

## 输出语义

左键有 m 行、右键有 n 行时，同键组写出 **m×n** 条配对：

* 没有匹配的左行仍保留一次，右侧为 `null`，`right_row` 为 `null`；
* 右边单独多出的行（右外部分）不输出；
* 每行格式：

```json
{"left_row": 1, "right_row": 2, "left": { ...原始载荷... }, "right": { ...原始载荷... }}
```

* 行号从 1 开始、按物理行计数；未匹配时 `right_row` 为 `null`；
* 最终严格按 `left_row`、再按 `right_row` 排序（null 排在同组最后；未匹配行
  本身没有其他配对）；
* 两条内容完全相同的输入仍是两条独立记录；
* `left`/`right` 保存**原始载荷**：嵌套对象、数组、大整数、浮点、布尔与 null
  原样往返，不被字符串化、不丢精度（JSON 对象成员顺序不作承诺）。

## 运行入口

```bash
export PYTHONPATH="$PWD"   # 或把仓库根目录加入 PYTHONPATH

python3 -B -E -m handover_join.cli \
  --left   examples/handover/facts.jsonl \
  --right  examples/handover/registry.jsonl \
  --config examples/handover/config.json \
  --output pairs.jsonl \
  --work-dir /tmp/join_scratch \
  --chunk-size 1000 --pair-chunk-size 1000 --fan-in 4
```

或直接：`bash examples/run_example.sh`（用 `chunk-size=2, fan-in=2` 强制小
数据集也走多轮归并）。退出码 2 表示输入/类型错误，错误信息带边别与行号。

参数：

| 参数 | 含义 |
|---|---|
| `--chunk-size` | 每个排序块最多缓冲的记录条数；满了就落盘成一个段 |
| `--chunk-bytes` | 每个排序块最多缓冲的帧字节数；达到即提前落盘（按条数限制之外的第二道闸，大载荷场景必用） |
| `--pair-chunk-size` | 配对结果外排时的块条数（默认同 `--chunk-size`） |
| `--pair-chunk-bytes` | 配对外排块的字节预算（默认同 `--chunk-bytes`） |
| `--fan-in` | 单轮归并最多同时打开的段数（≥2）；段更多时自动分多轮 |
| `--work-dir` | 临时段文件目录，作业结束自动清空 |

## 外存算法（为什么内存不随数据量增长）

1. **分段排序**：逐行读 JSONL → 逐分量编码复合键 → 每 `chunk-size` 条在内存
   排序后写成一个定长帧段文件（`frame.py`，自描述长度前缀）。
2. **有限路数归并**：段数超过 `fan_in` 时，按每批 `fan_in` 个段做 k 路堆归并，
   反复成轮（wave）直到只剩一个段。任何时刻打开的段文件数 ≤ `fan_in`，与总
   段数无关（测试 `TestFileHandleBound` 监控真实打开句柄数）。
3. **同键组外存配对**：对左右两个有序段做归并连接。遇到同键组时，只记录右组
   在文件中的**起止字节偏移**；然后逐行取左记录，每次把右组从磁盘重新流式
   读一遍生成配对并立即送入配对外排缓冲。即使所有数据挤在同一个热门键上，
   也不会把右组、左组或 m×n 个配对装进任何列表。
4. **恢复左侧顺序**：配对帧本身再做一次外排，排序键为
   `(left_row, right_row)`；不允许为保持原顺序把全部配对缓存在内存。最后流式
   解码成 JSONL 输出。

### 单条记录大小限制

帧格式的长度字段是无符号 32 位整数，硬上限为单帧 4 GiB；实际可用上限由内存
预算决定，而且更严格：**单条输入行序列化后必须能完整驻留内存**——读取与 JSON
解析天然按行进行，输出阶段同一条配对（一条左载荷 + 一条右载荷 + 解析后的两个
Python 对象）也要同时存在。因此实际单条记录上限应明显小于可用内存（例如
96 MiB 预算下，建议单条载荷控制在数 MiB 以内；若单行接近预算，应调小
`chunk-size` 到 1）。除此之外没有其他随数据增长的内存项。

### 内存构成（除单条载荷外均为有界常量）

| 组成 | 上界 |
|---|---|
| 排序块缓冲 | 同时受 `chunk_size` 条与 `chunk_bytes` 字节约束（键字节串 + 体字节串），与总行数无关 |
| 归并堆 | 每路仅 1 个头部帧，共 ≤ `fan_in` 个，与段数无关 |
| 连接阶段 | 当前 1 条左帧 + 重读右组时的 1 条右帧 + 2 个偏移量；同键组再大也不缓存 |
| 配对外排缓冲 | 同时受 `pair_chunk_size` 条与 `pair_chunk_bytes` 字节约束，与总配对数无关 |
| 文件缓冲 | 每个打开文件固定的 64 KiB 缓冲，打开数 ≤ `fan_in + 常数` |
| 单条记录 | 当前行的原始字节、解析对象与输出配对对象（见上节限制） |

即：除单条载荷外，内存只取决于 `chunk-size`、`pair-chunk-size`、`fan-in` 与
固定 Python 解释器开销，**不随总行数、段数或最大同键组增长**。磁盘占用约为
输入总量的若干倍（左右段 + 多轮归并中间段 + 配对段 + 最终输出），中间段在
消费后立即删除。

## 测试

```bash
bash scripts/run_tests.sh        # 等价 python3 -I -m unittest discover -s tests -v
```

小数据的期望输出由 `tests/naive_reference.py` 中的**独立朴素嵌套循环**逐行
构造（不共享生产代码），再与实际输出做包含顺序、行号与完整载荷的逐项比较。
全部用例以 `chunk-size=1/2/3、fan-in=2` 等极小参数运行，强制产生多轮归并。
覆盖：

* 多对多、未匹配左行、右外行不输出、空键/缺字段（含两边同时为空）；
* 左右字段名不同、三分量复合键、带分隔符字符串、原文大小写/空格敏感；
* 分隔符拼接串键与整数十进制拼接串键的反例（逐分量编码必须保持不同键）；
* 超大整数（10^40 量级，正负与嵌套场景）、嵌套载荷保真、布尔/浮点不是整数；
* 完全重复行仍为独立记录；空左表、空右表、双空表；CRLF 行尾；
* 类型错误报出边别与物理行号、坏 JSON 的行号、CLI 退出码 2 与临时文件清理；
* 多轮归并下的最终排序、字节预算强制落盘，以及并发打开文件数受 `fan_in` 约束。

## 压力演示（单一热门键，默认输入 > 256 MiB）

普通回归不生成大文件。压力数据规模全部可参数化，生成器逐行写盘，
**不会预先展开任何配对**：

```bash
# 全规模（约 258 MiB 输入，300×8 的单一热门键，输出约 2 GiB）
python3 -B -E scripts/stress_demo.py \
  --data-dir /tmp/hj_stress/data --work-dir /tmp/hj_stress/work \
  --output   /tmp/hj_stress/pairs.jsonl \
  --report   /tmp/hj_stress/report.json

# 快速冒烟（秒级）
python3 -B -E scripts/stress_demo.py --data-dir /tmp/smoke/data \
  --work-dir /tmp/smoke/work --output /tmp/smoke/pairs.jsonl \
  --left-hot 6 --right-hot 3 --row-bytes 1000 --chunk-size 2 --fan-in 2
```

作业在**独立子进程**中运行，Linux 下用
`resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` 测量作业自身峰值 RSS，超过
`--rss-limit-mib`（默认 96 MiB）即以非零码失败。报告记录输入规模、输出行数、
扇入、归并轮数、峰值 RSS 实测值与墙钟时间；输出文件再经流式校验（行数、严格
排序、null 语义），全程不把输出读入内存。实测结果见
`stress_results/stress_report.md`（另附两份归档 JSON 报告）。

## 目录

```
handover_join/
  keycode.py    # 配置校验、逐分量复合键编码（无分隔符拼接）
  frame.py      # 长度前缀二进制帧（段文件与配对假脱机格式）
  external.py   # 分段排序 + 有限扇入多轮归并（条数 + 字节双预算）
  job.py        # 端到端流水线：排序 → 归并连接（外存同键组配对）→ 配对外排 → JSONL
  cli.py        # 命令行入口
  errors.py     # 带边别/行号的错误类型
tests/          # unittest 回归（24 个用例）+ 朴素嵌套循环 oracle
scripts/        # 回归脚本、压力数据生成/子进程测量/一键演示
examples/       # 可直接运行的小样本（bash examples/run_example.sh）
stress_results/ # 压力实测报告与归档 JSON（含 257.5 MiB / 31.34 MiB RSS 数据点）
```
