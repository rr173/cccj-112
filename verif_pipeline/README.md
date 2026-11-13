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
| `data/obs/station_obs.csv` | `station_id,valid_time,precip_mm,version,issued_time`（多版本观测） |
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
reference/     独立 NumPy 参考实现（与生产零代码共享）
scripts/       数据生成、管线入口、参考核对入口、故障复现
tests/         可手算验证、冻结测试、观测修订测试、缺测有效性回归
configs/       分析配置（含冻结截止时间）
data/          本地合成输入（脚本生成）
outputs/       版本化评分输出（脚本生成）
```
