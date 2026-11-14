# 验收对照：需求点 → 夹具/演示位置

所有夹具均由 `tests/pcap_builder.py` 在测试运行时合成真实 PCAP，不依赖
外部文件、网络或抓包权限。运行方式：`./scripts/run_tests.sh`。

## 容器与链路层

| 需求点 | 夹具 | 测试文件 |
| --- | --- | --- |
| 经典 PCAP、微秒时间戳、大小端两种文件头 | 同一流分别以大端/小端写出，恢复字节一致 | `test_tcp_pcap.py::ContainerTests::test_both_endiannesses_recover_same_bytes` |
| linktype 仅 Ethernet | 非 1 linktype 致命拒绝 | `test_non_ethernet_linktype_is_rejected` |
| 纳秒 magic 不在范围内 | 纳秒 magic 致命拒绝 | `test_nanosecond_magic_is_rejected` |
| 全局头残缺 → 整体损坏、非零退出、不公布 | 8 字节文件 | `test_truncated_global_header_publishes_nothing` |
| 包头残缺 | 末尾砍 4 字节 | `test_truncated_packet_header_publishes_nothing` |
| 包记录残缺 | 末尾砍 7 字节 | `test_truncated_packet_record_publishes_nothing` |
| 坏 magic | 全零头 | `test_bad_magic_is_fatal` |
| 按实际 IP/TCP 头长跳过选项 | IHL=7、data offset=7 的流正确取载荷 | `test_variable_header_lengths_are_skipped_correctly` |

## 方向、四元组与连接生命周期

| 需求点 | 夹具 | 测试 |
| --- | --- | --- |
| 只跟踪配置端点的服务端→客户端方向 | 混入客户端方向包，连接数仍为 1 | `test_client_direction_is_ignored` |
| 完整四元组区分下载 | 演示中同服务器、不同客户端端口的两条连接 | `demo/run_demo.sh` |
| 选中方向遇 IP 分片 → 连接失败 | MF+偏移的分片帧 | `test_ip_fragment_fails_connection` (`ip_fragmented`) |
| 抓包截短 → 连接失败 | 帧在 TCP 载荷内被截断 | `test_capture_truncation_fails_connection` (`capture_truncated`) |
| SYN+ACK 定起点、SYN 占一个序号 | 全部重组用例的序号断言 | `test_wraparound_sequence_space` 等 |
| 无 SYN+ACK | 跳过握手包 | `test_no_synack_is_rejected` (`no_syn_ack`) |
| 无 FIN | 跳过 FIN 包 | `test_no_fin_is_rejected` (`no_fin`) |
| RST 结束 | 末包替换为 RST | `test_rst_termination_is_rejected` (`rst_terminated`) |
| 四元组复用、不同起点第二次握手 | 同一四元组两个不同 ISN | `test_reused_four_tuple_with_new_isn_is_rejected` (`connection_reused`) |
| 相同 SYN+ACK 重传可接受 | 连续两个同 ISN 握手 | `test_identical_synack_retransmit_is_allowed` |

## 序号空间与重组语义

| 需求点 | 夹具 | 测试 |
| --- | --- | --- |
| 32 位序号绕回 | ISN=`0xFFFFFFD0`，数据跨越 2³² 边界 | `test_wraparound_sequence_space` |
| 包倒序 | 数据段整体逆序写出 | `test_reverse_packet_order_with_early_fin` |
| FIN 早于前面数据包 | FIN 第一个写、握手最后写，仍判定完整 | 同上 |
| 完全重传 + 部分重叠且字节相同只计一次 | 同段重传 + 偏移 30 的重叠重传 | `test_identical_retransmit_and_partial_overlap_count_once` |
| 重叠区字节不同 → 连接不可信 | 重传改第 10 字节 | `test_conflicting_retransmit_fails_connection` (`byte_conflict`) |
| 不能拼接缺口两侧 | 丢掉一个中段 | `test_missing_segment_fails_without_joining_gap` (`missing_bytes`) |
| 重建长度上限 | `--max-stream-bytes 20` 对 64 字节流 | `test_stream_size_limit_is_diagnostic` (`stream_limit`) |
| 连接数上限 | 超出 `--max-connections` | 由 `Reassembler` 上限逻辑 + 演示双连接覆盖 |

## HTTP/1.1 分帧

| 需求点 | 夹具 | 测试 |
| --- | --- | --- |
| 一个流内多个响应 | chunked 响应紧接 Content-Length 响应 | `test_back_to_back_responses_including_second_object` |
| chunk 十六进制长度 + 分号扩展 | `5;name=value` 首块 | `test_chunked_with_extensions_and_trailers` |
| 零块后读完 trailer 与空行 | 两个 trailer 字段 | 同上 |
| 重复且一致的 Content-Length | 两个 `Content-Length: 4` | `test_duplicate_consistent_content_length_accepted` |
| 冲突 Content-Length | 10 与 11 | `test_conflicting_content_lengths_rejected` |
| chunked 与 Content-Length 同时声明 | 两头并存 | `test_chunked_and_length_together_rejected` (`conflicting_framing`) |
| 未知 Transfer-Encoding | `Transfer-Encoding: gzip` | `test_unknown_transfer_encoding_rejected` |
| 1xx 不输出对象、204/304 无正文 | 100 → 204 → 304 → 200，仅 1 个对象 | `test_provisional_then_204_then_304_produce_no_objects` |
| 无明确边界不靠关闭猜 | 只有 `Connection: close` | `test_body_without_boundary_is_rejected` (`unsupported_framing`) |
| 短正文 | Content-Length 99 实际 16 字节 | `test_short_content_length_body_rejected` |
| 坏 chunk 长度 | `zz` | `test_bad_chunk_size_rejected` |
| chunk 缺 CRLF | 块数据后接 `XX` | `test_missing_chunk_crlf_rejected` |
| chunk 数据被截断 | 声明 16 字节只有 5 字节 | `test_truncated_chunk_data_rejected` |
| 缺 CRLF/裸 LF | 状态行仅以 LF 结束 | `test_missing_crlf_after_status_line_rejected` |
| 出错后不搜索重新同步 | 好响应 + 坏响应 + 另一好响应，整体失败且无对象 | `test_no_resync_after_garbage` |
| 头部大小限制 | 200 字节头 + `--max-header-bytes 64` | `test_header_size_limit_enforced` |
| 字段名不区分大小写 | `cOnTeNt-LENGTH` | `test_case_insensitive_header_names` |

## 内容编码与上限

| 需求点 | 夹具 | 测试 |
| --- | --- | --- |
| 先去分块再解压 | 演示连接 A：chunked 包裹 gzip 成员 | `demo/run_demo.sh` + `GzipTests` |
| gzip CRC/ISIZE 校验 | 翻转 CRC 尾字节 | `test_bad_gzip_crc_rejected` (`gzip_corrupt`) |
| gzip 流损坏 | 伪造非 deflate 数据 | `test_bad_gzip_stream_rejected` |
| gzip 头字段 FNAME/FCOMMENT | 手工构造带头字段的成员并校验通过 | `test_gzip_with_fname_and_fcomment_headers` |
| 解压上限 | 1000 字节正文 + `--max-decoded-bytes 100` | `test_decompressed_size_limit_enforced` (`body_limit`) |
| 其他编码不支持 | `Content-Encoding: br` | `test_unsupported_content_encoding_rejected` |
| 无编码按原始字节 | 0–255 全字节序列逐字节守恒 | `test_identity_bytes_are_preserved_unchanged` |

## 发布契约

| 需求点 | 夹具 | 测试 |
| --- | --- | --- |
| 完整预期字节与偏移硬编码（非程序自报计数） | 57 字节小金夹具，偏移 0/54/54/57 写死 | `test_offsets_contract.py::GoldenOffsetTests` |
| 独立程序复核长度与 SHA-256 | 独立打开输出文件重算摘要 | `OutputContractTests::test_independent_output_verification`、`demo/verify_demo.py` |
| 连接内任一响应失败则不公布该连接 | 坏响应连接输出仅 `index.json` | `test_no_resync_after_garbage`、各 `expect_failure` |
| 其他完好连接仍提取 + 总索引含失败原因 + 退出码非零 | 双连接演示 | `demo/run_demo.sh`（真实退出码 1） |
| 结构损坏不公布任何结果 | 三类截尾后输出目录不存在 | `ContainerTests` 的 `..._publishes_nothing` |
| 输出目录必须是新目录 | 预先建目录后调用 | `test_existing_output_dir_is_refused` |
| 源 PCAP 不被改写 | 运行前后字节比对 | `test_source_pcap_is_not_modified` |
| 无匹配流量 | 端点配置指向不存在的 IP | `test_no_matching_traffic_exits_nonzero_with_index` |
| 配置校验 | 8 种非法配置 | `ConfigTests::test_bad_shape_and_values_are_rejected` |
