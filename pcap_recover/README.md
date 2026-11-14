# pcap_recover — 离线 PCAP 目录响应恢复命令

采集机留下了抓包但没有保存 HTTP 响应正文。`pcap_recover` 在**完全离线**
（不打开任何网络套接字、不发起任何连接）的条件下读取经典 PCAP 与采集端点
配置，重建服务端发往客户端的 TCP 字节流，解析其中的 HTTP/1.1 响应，解压
gzip 正文，并把每个最终响应连同可回溯到重建字节的元数据发布到一个新目录。

它是仓库中的独立新增包，不修改任何既有业务模块；除 Python 3.8+ 标准库外无
第三方依赖。

## 1. 支持边界（明确约定）

### 1.1 抓包容器

| 项目 | 支持情况 |
| --- | --- |
| 容器格式 | 经典 PCAP（magic `a1b2c3d4` / `d4c3b2a1` 两族） |
| 时间戳精度 | 仅微秒；纳秒 magic 直接拒绝 |
| 字节序 | 文件头大小端两种均支持，按 magic 自动判定 |
| 链路层 | 仅 Ethernet（linktype 1） |
| 网络层 | 仅 IPv4；其他 ethertype/协议跳过 |
| 传输层 | 仅 TCP；IPv4 IHL 与 TCP data offset 按线上头长跳过选项 |

**容器级损坏**（全局头/包头/包记录残缺、magic 无法识别、版本或 linktype
不支持）属于整个输入损坏：命令以退出码 `3` 终止，**不公布任何本次结果**，
截尾文件不会被当作正常 EOF。

### 1.2 方向与连接

* 配置文件列出允许采集的服务端 `(ip, port)`；只跟踪**从这些端点发往客户端**
  的方向，用完整四元组（客户端 IP/端口 + 服务端 IP/端口）区分下载；
* 客户端方向及无关流量直接跳过；
* 选中方向上遇到 **IPv4 分片**或**抓包截短**（IP 总长度/TCP 头长与实际捕获
  字节不一致）时，该连接以 `ip_fragmented` / `capture_truncated` 记录失败。

### 1.3 TCP 重组保证

* 每条连接必须有服务端 **SYN+ACK** 确定字节起点；SYN 占用一个序号，第一
  个数据字节映射为流偏移 0；
* 支持乱序、重传与部分重叠：相同序号上的相同字节只进入一次；重叠区域出现
  **不同字节**即判定 `byte_conflict`，整条连接不可信，不会挑选先到/后到
  版本；
* 支持 32 位序号绕回，单连接数据跨度约定小于 2³¹；
* 同一四元组在一个文件中不允许复用：出现起点 ISN 不同的第二次握手时以
  `connection_reused` 拒绝（相同 ISN 的握手重传视为正常重传）；
* **不按抓包顺序拼接**，也不会把缺口两侧直接相连；FIN 可以早于前面的数据
  包出现在文件中，完整性在整个抓包读完后才判定；
* 服务端 FIN 必须明确结束该方向。无 FIN（`no_fin`）、RST 结束
  （`rst_terminated`）、无 SYN+ACK（`no_syn_ack`）、FIN 前仍缺字节
  （`missing_bytes`）均记录失败。

### 1.4 HTTP/1.1 分帧

一个完整 TCP 流中可连续出现多个响应。字段名按 **RFC 7230 不区分大小写**
判断，原始头字段（含重复项）原样保留在元数据中。

* 正文边界只接受 `Content-Length`（十进制）与
  `Transfer-Encoding: chunked`（十六进制块长，支持分号扩展，零块后读完
  trailer 与空行才进入下一响应）；
* 重复且取值一致的 `Content-Length` 允许；冲突值
  （`conflicting_content_length`）、同时声明 chunked 与 Content-Length
  （`conflicting_framing`）、未知 Transfer-Encoding
  （`unsupported_transfer_encoding`）一律拒绝；
* `100–199` 临时响应、`204`、`304` 按无正文处理，不产生对象（仍计入响应
  序号）；其他响应若没有两种明确边界之一，报 `unsupported_framing`，
  **不靠 TCP 关闭猜正文**；
* 头部大小受限；短正文（`short_body`）、错误 chunk 长度
  （`bad_chunk_size`）、chunk 数据缺 CRLF（`bad_chunk_terminator`）、
  任何行缺 CRLF/裸 LF（`missing_crlf`）均拒绝；
* 出错后**不会**搜索下一段 `HTTP/1.1` 假装重新同步——整条连接失败。

### 1.5 内容编码

* 无 `Content-Encoding`：按原始字节保存，不假定文本编码；
* `gzip`：先去分块再解压；手动跳过 gzip 头字段（FNAME/FCOMMENT/FEXTRA/
  FHCRC），并显式校验尾部 CRC-32 与 ISIZE；CRC/ISIZE 不符、deflate 流损坏
  均为 `gzip_corrupt`，不会留下貌似成功的正文；
* 其他编码（如 `br`、`deflate`）报 `unsupported_content_encoding`。

### 1.6 资源上限（均可在命令行调整）

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--max-connections` | 1024 | 选中连接数上限 |
| `--max-stream-bytes` | 256 MiB | 单连接重建流长度上限 |
| `--max-header-bytes` | 64 KiB | 单响应头段大小上限 |
| `--max-decoded-bytes` | 256 MiB | 单响应解压后正文上限 |

超限是**可诊断的失败**（`connection_limit` / `stream_limit` /
`header_too_large` / `body_limit`），不会无上限占用内存。

## 2. 端点配置

```json
{
  "endpoints": [
    {"ip": "203.0.113.10", "port": 8080}
  ]
}
```

配置必须是含 `endpoints` 数组的 JSON 对象；IP 必须合法、端口在 1–65535、
条目不可重复。配置错误以退出码 `2` 终止。

## 3. 用法

```bash
python3 -m pcap_recover.cli \
    --pcap capture.pcap \
    --endpoints endpoints.json \
    --output ./recovered
```

`--output` 必须是**尚不存在的新目录**。结果先在同级隐藏暂存目录中组装，
最后一次 `rename` 公布：结构损坏时什么都不公布；连接级失败不会留下该连接
的半成品文件；源 PCAP 以只读方式打开，不会被改写。

### 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 所有选中连接全部成功 |
| `1` | 命令正常完成，但有选中连接失败（或没有任何流量匹配端点）；总索引已公布 |
| `2` | 配置/参数错误或资源上限中止 |
| `3` | 输入结构损坏，未公布任何结果 |

## 4. 输出契约

```
recovered/
├── index.json                     # 总索引：成功连接 + 失败原因
└── conn_<client>_<cp>__<server>_<sp>/
    ├── body_1.bin                 # 第 1 个对象的最终（解压后）正文
    ├── metadata_1.json
    ├── body_2.bin
    └── metadata_2.json
```

`metadata_N.json` 记录：四元组、HTTP 响应序号与对象序号、状态码、原因短语、
**原始头字段（保留重复项与原始大小写）**、TCP 流内起止偏移
（`response_start` / `header_end` / `body_start` / `body_end`，可直接对应
重建字节切片）、分帧方式、线上正文长度 / 去分块后长度 / 解压后长度、
`Content-Encoding` 与正文 SHA-256。

连接内**所有**响应验证通过后才公布该连接目录；任一响应失败，该连接前面
看似成功的对象也不会落盘。其他完好连接照常提取。

### 失败原因码一览

`byte_conflict`、`missing_bytes`、`no_syn_ack`、`no_fin`、
`rst_terminated`、`connection_reused`、`ip_fragmented`、
`capture_truncated`、`stream_limit`、`span_exceeds_2gb`、
`unsupported_framing`、`conflicting_framing`、
`conflicting_content_length`、`unsupported_transfer_encoding`、
`bad_chunk_size`、`bad_chunk_terminator`、`bad_trailer`、`short_body`、
`missing_crlf`、`header_too_large`、`gzip_corrupt`、
`unsupported_content_encoding`、`body_limit`。

## 5. 离线演示（两条并发下载，无需网络/浏览器/抓包权限）

```bash
./demo/run_demo.sh
```

脚本合成一个含两条同时下载连接的 PCAP：

* 连接 A（客户端端口 51001）：**chunked + gzip** 目录正文，紧接第二个
  Content-Length 响应；
* 连接 B（客户端端口 51002）：中段重传时**改变一个字节**。

真实结果：只公布连接 A 的两个对象；总索引把连接 B 标为 `byte_conflict`；
命令真实退出码为 `1`。`demo/verify_demo.py` 不导入本包实现，独立读取输出
复核全部长度与 SHA-256，并对第二个响应做逐字节（hex）比对。

## 6. 验收夹具与复现

```bash
./scripts/run_tests.sh
```

夹具全部由 `tests/pcap_builder.py` 现场合成真实 PCAP，覆盖：

* 序号跨 2³² 绕回、包倒序且 FIN 提早出现、完全重传与部分重叠（字节相同只
  计一次）、缺失段、无 SYN+ACK、无 FIN、RST、四元组复用、两种 PCAP 端序、
  可变 IPv4/TCP 头长（带选项）、IP 分片、抓包截短；
* 1xx 后接 204/304、矛盾 Content-Length、chunk 扩展与 trailer、坏 chunk
  长度/缺 CRLF、坏 gzip（CRC 篡改）与解压上限、未知编码；
* 一份**硬编码完整预期字节与偏移**的小金夹具（`GoldenOffsetTests`），预期
  值直接写在测试源码中而非由程序计数派生；
* 独立复核程序重新计算每个正文的长度与摘要；
* 容器截尾（全局头/包头/记录）不公布任何结果、输出目录已存在时拒绝写入、
  源 PCAP 字节不变。

## 7. 目录结构

```
pcap_recover/
├── pcap_recover/        # 实现包（标准库 only）
│   ├── pcap.py          # 经典 PCAP 容器读取与结构校验
│   ├── netparse.py      # Ethernet/IPv4/TCP 头解析
│   ├── reassembly.py    # 序号绕回/乱序/重叠/FIN 语义
│   ├── http.py          # 严格 HTTP/1.1 分帧与去分块
│   ├── encoding.py      # gzip 校验式解压
│   ├── config.py        # 端点配置
│   ├── extract.py       # 流水线、原子发布、总索引
│   ├── cli.py           # 命令行入口
│   └── errors.py
├── tests/               # unittest 验收夹具（59 个用例）
├── demo/                # 两连接离线演示与独立复核
├── scripts/run_tests.sh
└── docs/acceptance.md   # 需求点 → 夹具对照
```
