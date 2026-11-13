# 集合降水预报概率验证管线

可复算的集合预报验证数据管线：读取本地 NetCDF 集合预报与站点观测，输出按
**起报周期 × 预报时效 × 区域 × 阈值** 拆分的概率评分（Brier score、可靠度分箱、
ensemble CRPS）。全部输入为本地合成文件，不访问任何在线气象服务。

## 快速开始

```bash
# 依赖：Python 3.11+，numpy，netCDF4，pytest（仅测试需要）
python3 scripts/make_synthetic_data.py        # 生成本地合成数据到 data/
python3 scripts/run_pipeline.py               # 运行管线，输出到 outputs/<name>_<snapshot_id>/
python3 scripts/run_reference_check.py        # 用独立 NumPy 参考实现核对生产输出
python3 -m pytest tests/ -v                   # 运行测试（含可手算验证）
```

复现与回归（缺测值有效性传递）一条命令：

```bash
python3 scripts/repro_validity_bugs.py && python3 -m pytest tests/ -q
```

`repro_validity_bugs.py` 用两个最小案例（`_FillValue=9999` 被当作有效雨量、
manifest 成员缺文件导致应有分母缩水）直接核对管线输出，任一期望不满足即以
非零码退出。

每次运行把全部结果写入以内容快照号命名的目录（如
`outputs/demo_f7c9d226e71ea4cc/`）：相同冻结输入永远得到相同快照号与相同文件。

## 输入

| 文件 | 说明 |
|---|---|
| `data/forecast/manifest.csv` | 产品清单：`path,cycle,member,priority,run_id` |
| `data/forecast/*.nc` | 每（周期， 成员）一个 NetCDF；变量 `apcp(time,lat,lon)` 为**累计降水量**(mm)，全局属性 `cycle/member/reset_interval_hours`；缺测以 `_FillValue` / `missing_value` 属性或非有限值表示，变量可带 `scale_factor`/`add_offset` 打包（哨兵值以存储单位给出） |
| `data/obs/stations.csv` | `station_id,lat,lon,region,weight`（区域与固定站点权重） |
| `data/obs/station_obs.csv` | `station_id,valid_time,precip_mm,version,issued_time`（多版本观测；可由 `station_obs_telemetry.csv` 由遥测导出，见下文） |
| `configs/*.json` | 分析配置：`init_cutoff`、`obs_cutoff`、阈值、分箱宽度、负差容差 |

## 声明的约定（不可静默更改）

1. **分析冻结**：一次分析固定起报截止时间（`init_cutoff`，只纳入周期时刻 ≤ 截止的
   起报周期）与观测版本（`obs_cutoff`，只用签发时间 ≤ 截止的版本）。每个
   （站点， 有效时刻） 取 ≤ 截止的最高版本；`(version, issued_time)` 完全并列时
   以文件中靠后的行为准。观测修订只影响其涉及的有效时刻；截点之后的更正
   不改变已冻结分析（由 `tests/test_freeze.py`、`tests/test_obs_revision.py` 验证）。
2. **重复文件**：同一 （周期， 成员） 有多个文件时，取清单中 `priority` 最小者，
   并列取路径字典序最小者；未采用项连同原因（`duplicate_lower_priority` /
   `file_missing` / `cycle_after_init_cutoff`）写入 `unused_files.csv`。
3. **累计→区间**：对同一周期内相邻时效 `s < t`：若 `s == 0`（周期起点）或
   `s % reset_interval_hours == 0`（累计周期重置点），区间量 = `apcp(t)` 本身；
   否则区间量 = `apcp(t) − apcp(s)`。差值 `< −1e-6 mm` 判为**负差异常**（区间
   置为缺测，该成员在该时效不计入有效分母）；`[−1e-6, 0)` 视为舍入噪声截断为 0。
   重置、截断、异常全部记录于 `accum_events.csv`。累计量绝不当逐时量使用。
4. **缺测传播**：文件解码时识别 `_FillValue`、`missing_value`（按存储单位比较，
   先于 unpacking）与非有限值，打包变量（`scale_factor`/`add_offset`）先掩码再
   解包——特殊占位符绝不参与数值运算。缺测以最小范围传播：缺测的 `apcp(t)`
   只使该区间缺测（`missing_input`）；相邻差分需要两端可用，前一时刻缺测使
   该区间缺测（`missing_previous`）；计数器归零后的新段凭合法新读数恢复。
   两类缺测事件同样记录于 `accum_events.csv`。
5. **空间插值**：站点以双线性权重映射到周围格点，权重显式存储在
   `station_probs.csv` 的 `interp_weights` 列；任一**正权重**贡献格点缺测则
   站点值缺测（零权重邻点不参与、不影响站点）；网格外站点钳制到最近边界。
6. **有效成员分母**：事件概率 = 满足 `成员区间量 >= 阈值` 的**有效成员数 /
   实际有效成员数**（异常、缺测、缺文件成员不占分母）。**应有成员集** =
   `init_cutoff` 合格清单行（周期 ≤ 截止）声明的成员并集，在文件存在性检查
   之前固定；缺文件或坏格点只减少可用数，绝不缩小应有数。成员覆盖比例 =
   有效成员数 / 应有成员数，逐案例输出。
7. **阈值事件**：一律采用 `>=` 约定（区间降水 >= 阈值即事件发生，边界值计入）。
8. **Brier score**：`(prob − obs)²` 的站点加权平均。
9. **可靠度分箱**：固定边界 `0, 0.1, …, 1.0`（宽度可配），左闭右开
   `[lo, hi)`，顶箱 `[0.9, 1.0]` 闭；距下边界 1e-9 内的概率归入该下边界所在箱
   （左闭规则的浮点稳健实现）。空箱输出缺测统计、`n_cases=0`。
10. **Ensemble CRPS**：能量形式
    `CRPS = (1/M)Σᵢ|xᵢ−y| − (1/(2M²))ΣᵢΣⱼ|xᵢ−xⱼ|`，M 为有效成员数。
    并列值无需特别处理：成对双重求和对任意重复值精确成立（参考实现改用
    排序恒等式独立验证，并列值承载相邻系数，结果一致）。
11. **区域汇总**：组内按固定站点权重加权平均（权重归一化在组内进行）；
    **空组返回缺测（空字段），绝不返回零分**。
12. **快照版本**：快照号 = 冻结内容（配置、清单、站点表、选中预报文件内容
    哈希、选中观测版本内容）的 SHA-256 前 16 位。相同冻结输入 → 相同快照号；
    截点后观测更正不改变快照号。

## 输出文件（`outputs/<name>_<snapshot_id>/`）

| 文件 | 内容 | 追溯链 |
|---|---|---|
| `scores_threshold.csv` | Brier score，按周期×时效×区域×阈值 | 聚合自 `station_probs.csv` |
| `reliability.csv` | 可靠度分箱（每组全部箱，含空箱） | 同上 |
| `scores_crps.csv` | ensemble CRPS，按周期×时效×区域 | 聚合自成员级值 |
| `station_probs.csv` | 逐案例概率、事件、成员数、覆盖率、观测版本、**插值权重** | 聚合自 `station_member.csv` + `obs_selected.csv` |
| `station_member.csv` | 逐成员站点插值区间降水量 | 聚合自 `grid_intervals.csv` |
| `grid_intervals.csv` | 格点级累计量、区间量、差分标志 | 来自 NetCDF `apcp` |
| `accum_events.csv` | 周期重置 / 负差截断 / 负差异常 / 缺测输入 / 前时刻缺测事件 | 差分时记录 |
| `obs_selected.csv` / `obs_excluded.csv` | 冻结采用的观测版本 / 未采用项及原因 | 版本选择 |
| `selected_files.csv` / `unused_files.csv` | 采用/未采用的预报文件及原因、内容哈希 | 清单选择 |
| `snapshot.json` | 快照号、配置、全部输入内容哈希、冻结统计 | 可复算性 |

任一聚合数值可沿 `scores_*.csv → station_probs.csv → station_member.csv →
grid_intervals.csv`（成员、插值权重）与 `obs_selected.csv`（观测版本）逐级追溯。

## 独立参考核对

`reference/reference_impl.py` 与生产包**零代码共享**，从原始输入用直接公式
重算全部评分并与生产输出比对（容差 1e-9）：CRPS 用排序恒等式
`CRPS = (1/M)Σ|xᵢ−y| − (1/M²)Σᵢ(2i−M−1)x₍ᵢ₎`（对并列值精确），
分箱、差分、插值、聚合均为独立循环实现。两处关键的独立推导保证交叉核对
不会与生产代码"同错"：

* **应有成员集**只从清单合格行推导（与哪些文件可读无关），并逐案例核对
  `n_valid_members` / `n_expected_members` / `member_coverage`——两份实现
  不可能再共用同一个错误分母而通过核对；
* **哨兵解码**（`_FillValue` / `missing_value` / 非有限值、打包变量）由参考
  实现自行完成——占位符若被拿来评分，站点级概率与有效数立即不匹配。

`tests/test_validity.py` 用变异测试证明这两点：把生产输出改回旧故障的数值
（分母缩到可读文件、9999 被评分），参考核对必然报错。
`scripts/run_reference_check.py` 任一不匹配即以非零码退出。

## 可手算验证（`tests/`）

`tests/fixture.py` 构造 3 成员 × 2+1 站 × 3 时效的小集合，全部期望值手工推导
（见 `tests/test_handcalc.py` 注释与 fixture 文档串）：

- 累计→区间差分（`16−4=12`）与周期重置（取重置后值 `2`，而非 `2−16`）
- 负差异常 → 该成员缺测 → 有效分母 3→2、覆盖率 2/3（站点 C 不受影响仍为 3/3）
- 阈值边界：区间量恰为 10 mm，`>=` 计入事件
- 概率分箱：`p=0.5` 入 `[0.5,0.6)`、`p=0` 入首箱、`p=1` 入闭顶箱
- 并列成员 `{2,10,2}`、`{5,4,5}` 的 CRPS 手算值（如 `11/9`、`28/9`）
- 区域固定权重 0.75/0.25 加权（如 BS `7/36`、CRPS `53/36`）
- 空组（无观测区域）返回缺测而非零分
- 冻结保证：相同输入快照号一致；截点后更正不改变已冻结分析；
  截点前修订只改变涉及有效时刻的评分组

`tests/validity_fixture.py` + `tests/test_validity.py` 构造三套缺测回归数据
（期望值同样全部手工推导，见 fixture 文档串）：

- 局部缺：单格点 `_FillValue=9999` 只淘汰依赖它的站点与时效
  （站点 C 不受影响；`missing_input` 事件逐格点记录）
- 相邻段传播：无重置时缺测读数使相邻两个区间缺测（`missing_previous`）
- 归零恢复：重置点凭合法新读数恢复（成员 2 在 18 h 重新有效，区间量 6）
- 全缺：整成员缺测不计入任何分母；全案例零有效成员时概率与评分为缺测、
  `n_cases=0`，绝不返回零分
- 应有分母：manifest 声明成员 1–4、4 号文件缺失 → `n_expected_members=4`、
  覆盖率 0.75；区域 BS/CRPS 手算值（如 `25/72`、`119/36`）
- 零权重邻点：站点恰在格点上时，相邻坏格点不影响该站点
- 解码单元测试：`_FillValue`、`missing_value`、`inf`/`nan`、
  `scale_factor`/`add_offset` 打包变量
- 变异测试：把输出改回旧故障数值，独立参考核对必须报错

## 项目结构

```
pipeline/      生产管线（config/io_forecast/io_obs/accum/interpolate/scores/snapshot/runner）
telemetry/     雨量站遥测（frame 编解码 / protocol 校验 / store SQLite / server TCP / gateway 模拟 / export）
reference/     独立 NumPy 参考实现（与生产零代码共享）
scripts/       数据生成、管线入口、参考核对入口、故障复现、遥测服务与导出
tests/         可手算验证、冻结测试、观测修订测试、缺测有效性回归、遥测协议/存储/导出/TCP 联调
configs/       分析配置（含冻结截止时间）
data/          本地合成输入（脚本生成）
outputs/       版本化评分输出（脚本生成）
```

## 雨量站遥测入口（TCP 网关 → SQLite → 观测 CSV）

观测除了人工整理的 CSV，还可以由现场网关通过 TCP 直接接入。整条链路
只在本机运行，无消息中间件、无站外账号；接收进度存在 SQLite 中，进程
或机器重启后接着收。

```
遥测网关 ──TCP(A55A 帧协议)──> telemetry_server ──> SQLite(接收进度)
                                                       │
                                    scripts/export_observations.py（验收 + 3h 汇总 + 十进制乘法）
                                                       ▼
                                        station_obs_telemetry.csv (+ .gaps.csv)
                                                       │
                                             现有 run_pipeline.py / 评分
```

### 协议（两端对测固定）

每帧：`A55A` + 两字节大端正文长度 + UTF-8 JSON 正文 + 四字节大端 CRC32，
正文长度 1–4096 字节；**CRC32 按「长度字段 + 正文」计算**。TCP 可能拆帧或
粘帧，接收端按字节流增量解码：半帧绝不当作完整消息；坏 CRC、非法长度、
垃圾字节都只丢弃当前帧并重新扫描 `A55A`，未解析缓冲上限 `--max-buffer`
（默认 1 MiB）。每个**合法**帧回一行 JSON，`ack/status/message_type/
station_id/batch_id` 是固定字段，NACK 另带 `reason` 与 `detail`；坏帧没有
可信身份，不回复（拒收原因写入 `--journal`）。

正文三种消息，身份统一为 `station_id + batch_id`：

| type | 字段 |
|---|---|
| `begin` | `start_ms,end_ms,count,tip_mm,sha256`；UTC Unix 毫秒，端点整秒，半开区间 `[start,end)`，`start<end`，`count<=100000`，`tip_mm` 为正十进制字符串 |
| `tip` | `seq,time_ms,tips`；`0<=seq<count`，`1<=tips<=65535`，`time_ms` 落在声明区间内 |
| `seal` | 仅身份；请求验收该批 |

摘要原文：按 `seq` 升序，每条拼成 ASCII `"seq,time_ms,tips\n"` 后逐条连接，
取 SHA-256 十六进制小写。`count=0` 允许空摘要（=`sha256("")`），表示该段确实无雨。
只接收 `stations.csv` 已登记的站。

### 接收进度 vs 可用于分析的数据

* `begin`/`tip` 事务提交后才回 ACK；相同身份相同内容重发 → ACK（`duplicate:true`）
  但不再插行；同身份不同内容 → NACK（`identity_conflict`/`tip_conflict`）并保留
  第一次内容。重复判断在 `BEGIN IMMEDIATE` 事务内完成，两个连接同时重发结果一致。
* `tip` 先于 `begin` → NACK `unknown_batch`，绝不凭空建批次。
* `seal`：漏号 → NACK `incomplete`（`missing_ranges` 精确到每段）；摘要不符 →
  NACK `digest_mismatch`，批次保持未验收；同站新批与已验收批的覆盖区间相交 →
  NACK `interval_conflict`（相邻端点不算重叠）。全部记录与摘要核对通过才写入
  `accepted_batches`，此时数据才可用于导出。重复 `seal` 安全（ACK + `duplicate`）。

### 导出与区间完整性

```bash
python3 scripts/export_observations.py --db runtime/telemetry.sqlite3 \
    --stations data/obs/stations.csv \
    --start 2026-02-01T00:00:00Z --end 2026-02-01T06:00:00Z \
    --issued-time 2026-02-02T00:00:00Z \
    --output data/obs/station_obs_telemetry.csv
```

* 起止必须是 UTC 三小时整点；脉冲按 `[t, t+3h)` 归桶，桶端点为 `valid_time`
  （00–03 时观测 → `03:00Z`）。恰在 03:00Z 的脉冲归 03–06 桶。一个批可跨多个桶。
* 用 `decimal.Decimal` 对 `tip_mm` 字符串做乘法累加，0.2 mm × 3 = `0.6`，没有二进制尾差。
* **只有该站三小时被已验收覆盖区间完全铺满才写观测行**；全覆盖且零脉冲写 `0`。
  有空档、或只有未验收批次 → 不写行，并在 `<output>.gaps.csv` 列出缺的
  `missing_start/missing_end`（`reason`：`partial_gap`/`unaccepted_batch_only`/
  `no_accepted_coverage`，以及相关 `pending_batch_ids`）。多个相邻已验收批可
  联合证明完整；「没收到包」不等于「没有雨」。
* CSV 五列 `station_id,valid_time,precip_mm,version,issued_time`，`version` 固定 1；
  每次导出**重写**文件（临时文件 + `os.replace`），不追加。导出只以只读方式打开 SQLite。

### 一条命令的本地联调

```bash
python3 scripts/run_telemetry_analysis.py   # 起服务→模拟网关→导出→现有分析→独立参考核对
python3 scripts/telemetry_demo.py           # 协议/持久化/并发/导出的 63 项断言
python3 -m pytest tests/telemetry/ -q       # 同样的场景以单测形式重跑
```

`run_telemetry_analysis.py` 生成 `data/obs/station_obs_telemetry.csv` 与
`runtime/analysis/config.json`，跑完既有管线后调用 `reference/reference_impl.run_check`
交叉核对（0 mismatch 才算过）。导出观测的 `valid_time` 落在数据集的
`2026-02-01T06:00:00Z` 预报时效上，因此 `station_probs.csv` 里 S1/S2/S3 的
`obs_interval_mm` 分别为 1.8 / 0.6 / 0，S4 该时效没有可用覆盖、观测列为空。`telemetry_demo.py` 把数据库断言、导出值、
实际收发记录（`runtime/demo/gateway_journal.jsonl`）留在 `runtime/demo/`
（`demo.log`、`summary.json`、`telemetry.sqlite3`），可直接查看。

联调覆盖：同一批拆帧（逐字节）、粘帧（一次写入多帧）、乱序与重复发送；夹入
坏 CRC、非法长度和垃圾字节后继续送合法帧；留一个 seq 空缺使 `seal` 失败并按
报告的缺号补齐；事务已提交但 ACK 未发出时进程退出（`--drop-ack-after N` 注错钩子，
进程以 77 退出），重启后网关重发不翻倍；两连接对同一 seq 发不同内容；并发
seal 两个覆盖重叠的批次；以及 0.2 mm 翻斗、跨 03:00Z 的脉冲、完整无雨段、
仅缺一秒覆盖的导出值核对。

**注意观测冻结约定**：分析只采用 `issued_time <= obs_cutoff` 的版本
（`configs/demo_analysis.json` 的 `obs_cutoff` 为 `2026-02-03T00:00:00Z`）。
导出时 `--issued-time` 必须不晚于该截点，否则导出的行会被冻结分析整体排除；
`scripts/check_telemetry_export.py configs/demo_analysis.json <obs.csv>` 检查
表头、版本、站点登记、三小时端点与签发时间。
