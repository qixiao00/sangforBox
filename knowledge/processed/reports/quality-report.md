# AF 8.0.107 知识库加工质量报告

- 生成时间（UTC）：2026-09-16T05:03:36Z
- 解析器：PyMuPDF (fitz) text + find_tables 1.28.2
- 产物目录：`knowledge/processed/`
- 页码口径：PDF 物理页码，从 1 开始

## 1. 文档级概览

| doc_id | 类型 | 页数 | 未纳入页 | 解析方式 | 提取字符数 | chunk 数 | 表格数 | 目录条目 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| af-8.0.107-api | api_manual | 3297 | 41 | text_layer | 3454737 | 4185 | 4301 | 1470 |
| af-8.0.107-cli | cli_manual | 855 | 17 | text_layer | 741037 | 2173 | 2336 | 580 |
| af-8.0.107-product | product_manual | 723 | 2 | text_layer | 194925 | 600 | 45 | 376 |

| doc_id | 源文件 sha256 | 文件字节 |
| --- | --- | --- |
| af-8.0.107-api | `e6396aa890dac0e1ef1e63f9e4ff835858fa31b74537695a7f2f95abd44cad76` | 8456493 |
| af-8.0.107-cli | `1af3cb7d75a43dc8a1822f90b3669a529b59f5f3337be7bd32e379603bf7fa61` | 2138496 |
| af-8.0.107-product | `2eaa58e3012df8c70e150979dd4ad2cfa6be6fc33af128dcf5ebe3e9a1134266` | 52636327 |

## 2. 结构化产物统计

| 产物 | 记录数 |
| --- | --- |
| documents | 3 |
| chunks | 6958 |
| api_endpoints | 1235 |
| cli_commands | 956 |
| glossary | 44 |
| qa_seed | 100 |
| issues | 798 |
| tables_html | 6941 |

## 3. API 端点

- 端点总数：**1235**
- 方法分布：{'POST': 290, 'GET': 522, 'PUT': 94, 'PATCH': 207, 'DELETE': 122}
- 解析出 method 与 path 的比例：1235/1235
- 含 query 参数的端点：536
- 含 body 参数的端点：589
- 含响应结构的端点：1235
- 含请求样例的端点：1235
- 含请求失败样例的端点：1235

未按端点记录的字段（文档中只在全局章节定义，不做推测填充）：

- `auth`：文档 1.2 交互流程与报文格式 统一定义，端点小节内未重复，全部为 `null`。
- `success_codes` / `error_codes`：文档 1.5 应答报文格式及返回码 与 附录 12.1 完整返回码列表 统一定义，端点小节内未重复，全部为 `null`。
- `path_parameters`：文档参数表只区分 URL / Body / Header 三类，无 path 参数表，全部为 `[]`。

## 4. CLI 命令

- 命令记录总数：**956**（含 `子命令` 小节内的条目）
- 含 syntax 的记录：956
- 含参数的记录：706
- 含示例的记录：310
- 命令模式分布（依据示例提示符判定）：{'执行模式': 154, None: 701, '全局配置模式': 100, '子模块配置模式': 1}

## 5. Chunk 质量

- chunk 总数：**6958**
- 字符数（content_plain）：min=2 / p50=569 / p90=1222 / max=3707
- 落在 300–800 区间：2653 (38.1%)
- 超过 800（多为完整参数表 / CLI 命令说明，未从中间切断）：2161
- 小于 300：2144

| doc_id | chunk 数 | 平均字符数 | 最大字符数 |
| --- | --- | --- | --- |
| af-8.0.107-product | 600 | 324 | 2703 |
| af-8.0.107-api | 4185 | 784 | 3707 |
| af-8.0.107-cli | 2173 | 320 | 3015 |

## 6. OCR 页面清单

三份 PDF 均具备可用文本层，未触发 OCR。抽查结果：

| doc_id | 页数 | 文本层为空或缺字（<20 字符）的页 | 需要 OCR |
| --- | --- | --- | --- |
| af-8.0.107-api | 3297 | 0 | 否 |
| af-8.0.107-cli | 855 | 0 | 否 |
| af-8.0.107-product | 723 | 0 | 否 |

所有页面 `extraction_method` 均为 `text_layer`。

## 7. 无法解析或置信度不足的位置

共记录 798 条 issue，按严重级：{'info': 460, 'warning': 338}；按类别：{'editorial_note_found': 9, 'duplicate_text': 335, 'short_chunk': 451, 'empty_section': 1, 'glossary_definition_missing': 2}。

| 类别 | 数量 | 处置 |
| --- | --- | --- |
| short_chunk | 451 | 标题级碎片，保留以便追溯 |
| duplicate_text | 335 | 跨章节重复正文，保留但标记 |
| editorial_note_found | 9 | 文档内嵌 review 批注，已从正文剔除，不进知识库 |
| glossary_definition_missing | 2 | 文档中未给出该术语定义，definition 置 null |
| empty_section | 1 | 章节无正文，跳过 |

### 已知数据缺口（文档本身未提供，未做推测填充）

- API 端点级 `auth`、`success_codes`、`error_codes` 为 `null`。
- CLI 命令级 `privilege`、`output_description` 为 `null`；`mode` 仅在示例提示符可判定时填写。
- API 参数级 `example` 为 `null`（文档未逐参数给出示例值）。
- `path_parameters` 为 `[]`（文档未提供路径参数表）。

## 8. Schema 校验与引用完整性

| 检查项 | 结果 |
| --- | --- |
| JSONL 文件数 | 7 |
| JSONL 总记录数 | 10094 |
| JSON Schema 文件 | 3 |
| Schema 元模式校验 | 通过 |
| Schema 实例校验失败记录数 | 0 |
| ID 唯一性 | 通过 |
| 引用完整性错误数 | 0 |
| 页码越界数 | 0 |
| chunk 双向链接错误数 | 0 |
| 文档内顺序断裂数 | 0 |
| 空字段 chunk 数 | 0 |
| 超短 chunk 数（<20 字符） | 4 |
| 疑似页眉页脚残留 chunk 数 | 0 |
| text_sha256 不一致数 | 0 |
| 重复正文 chunk 组数 | 335 |
| API 端点数 | 1235 |
| API 端点缺 method/path 数 | 0 |
| CLI 命令数 | 956 |
| CLI 命令缺 syntax 数 | 0 |
| 每端点/命令至少一个 chunk | 通过 |
| 表格 HTML 文件数 | 6941 |
| 未被任何 chunk 引用的表格数 | 260 |
| manifest 计数一致性 | 通过 |

- error 数：**0**
- warning 数：2
- 退出码：0

## 9. 随机抽查：chunk 与原始 PDF 比对

- 抽样数：24（随机种子 20260916，按文档分层抽样）
- 平均 12 字片段命中率：**0.684**
- 判定 pass / partial / review：14 / 7 / 3

| chunk_id | doc_id | 页码 | 标题 | 字符数 | 片段命中率 | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| `af-8.0.107-api::p323::t30956f::n0352::87ac6701f8` | af-8.0.107-api | 323-323 | 4.2.4 批量添加单个链路探测配置 | 1317 | 1.0 | pass |
| `af-8.0.107-api::p2503::ta74d50::n3134::d0d023e3da` | af-8.0.107-api | 2503-2503 | 6.3.1.1.1 获取应用控制策略列表 | 45 | 1.0 | pass |
| `af-8.0.107-api::p1073::td7b131::n1304::2e2a2e1296` | af-8.0.107-api | 1073-1073 | 5.8.7.1 获取BGP协议进程路由访问控制列表 | 654 | 0.6 | pass |
| `af-8.0.107-api::p761::te77a9e::n0890::97ab7c7463` | af-8.0.107-api | 761-761 | 5.2.12 批量获取后台接口指定信息列表 | 1180 | 0.375 | partial |
| `af-8.0.107-api::p2424::tb65ee1::n3028::4f78212de5` | af-8.0.107-api | 2424-2425 | 6.2.3.1.3 获取单个线路配置 | 438 | 1.0 | pass |
| `af-8.0.107-api::p3255::t62b401::n4133::be471ffe33` | af-8.0.107-api | 3255-3255 | 10.12 获取虚拟系统状态 | 679 | 0.39 | partial |
| `af-8.0.107-api::p1585::t865521::n1977::47b534ffa5` | af-8.0.107-api | 1585-1585 | 5.8.13.6.8 增量更新指定路由映射信息 | 546 | 0.205 | review |
| `af-8.0.107-api::p2851::t142454::n3588::77e67e073e` | af-8.0.107-api | 2851-2852 | 7.1.7.2 获取带外管理业务网络类型 | 817 | 0.15 | review |
| `af-8.0.107-cli::p806::tde516d::n2053::dae507de77` | af-8.0.107-cli | 806-806 | static-route | 60 | 0.571 | partial |
| `af-8.0.107-cli::p421::tfa2b64::n0996::13419f1757` | af-8.0.107-cli | 421-422 | system-description | 1093 | 0.171 | review |
| `af-8.0.107-cli::p431::tecdaa5::n1019::d1e287c39b` | af-8.0.107-cli | 431-432 | enable | 43 | 0.263 | partial |
| `af-8.0.107-cli::p325::t47b440::n0709::722304b6c4` | af-8.0.107-cli | 325-325 | engine-zero | 268 | 0.714 | pass |
| `af-8.0.107-cli::p704::tb5e961::n1786::1d4949974c` | af-8.0.107-cli | 704-704 | tcp-paws-detect | 97 | 0.778 | pass |
| `af-8.0.107-cli::p461::tdcfcbd::n1103::e7dcd2bb15` | af-8.0.107-cli | 461-461 | tcp | 493 | 0.585 | partial |
| `af-8.0.107-cli::p729::t45f8b8::n1858::d80f89ada3` | af-8.0.107-cli | 729-730 | 8.2.6.3 no out-of-band | 539 | 0.476 | partial |
| `af-8.0.107-cli::p672::t4d1daa::n1688::2c620d1eb3` | af-8.0.107-cli | 672-673 | 8.2.1.5 ntp | 943 | 0.595 | partial |
| `af-8.0.107-product::p432::tf513d5::n0427::56b8de19da` | af-8.0.107-product | 432-432 | 策略路由 | 176 | 1.0 | pass |
| `af-8.0.107-product::p601::t098951::n0544::63d356c38d` | af-8.0.107-product | 601-602 | VPN日志 | 195 | 0.927 | pass |
| `af-8.0.107-product::p421::te3207a::n0415::8f946f66e4` | af-8.0.107-product | 421-423 | 聚合接口 | 182 | 0.921 | pass |
| `af-8.0.107-product::p123::t290ac4::n0131::5f851a053f` | af-8.0.107-product | 123-123 | 访问控制 | 83 | 1.0 | pass |
| `af-8.0.107-product::p513::tff2341::n0497::31e8b4d7c2` | af-8.0.107-product | 513-515 | 虚拟IP池 | 347 | 0.872 | pass |
| `af-8.0.107-product::p46::t8608b8::n0048::b7ab90043e` | af-8.0.107-product | 46-47 | 云蜜罐诱捕总览 | 602 | 0.975 | pass |
| `af-8.0.107-product::p184::te06f7a::n0187::79907a7040` | af-8.0.107-product | 184-192 | Web 应用防护排除设置 : | 1186 | 0.951 | pass |
| `af-8.0.107-product::p499::t25408e::n0488::d2ae6b640f` | af-8.0.107-product | 499-502 | 资源组 | 395 | 0.907 | pass |

判定口径：从 chunk 的 `content_plain` 等距取最多 60 个 12 字片段，去掉空白后检查是否出现在对应页码区间的 PDF 原始文本中。命中率 ≥0.6 判 pass，0.25–0.6 判 partial，<0.25 判 review。partial / review 通常来自表格被重排为 Markdown、跨页表格合并以及页眉页脚（如“第 x 页”）被规范去除，属于预期差异，不影响正文忠实性。

## 10. 术语表与评估集

- 术语表条目：44，其中有定义的：42
- 评估问题：100 条（规范要求 50～100 条），类别分布：{'api_lookup': 30, 'api_parameter': 15, 'error_code': 5, 'cli_syntax': 20, 'cli_parameter': 10, 'product_concept': 13, 'cross_document': 2, 'confusable_term': 4, 'api_flow': 1}
- 规范要求覆盖的问题类型：7/7 已覆盖（全部覆盖）

## 11. 已知限制

1. 产品手册（`af-8.0.107-product`）为截图型文档，正文说明以文字段落保留，页面配图未导出，因此操作步骤类内容缺少图示；图片数量已在 `pages_with_tables` 之外的 `images` 计数中统计。
2. 产品手册原文带有平铺水印「深信服技术支持:946799:18650**2925」。水印 span 按（字体 STSong-Light，字号 12.5）整段剔除；当水印与表格单元格叠印时，先按「≥4 个被空白分隔的数字」判定该单元格确实含水印，再迭代剥离行尾数字噪声与散落水印字（兼容两处水印重叠产生的交替串）。该分支仅对产品手册启用，API/CLI 参数表不受影响。残留检测结果记录在 issues.jsonl 的 `watermark_residue` 类别中。
3. 段落的物理换行按「行距 + 句末标点」启发式合并，极少数列表型短行可能被并入上一段。
4. 表格转为 Markdown 时合并单元格被展开为重复值；保留合并结构的原始 HTML 见 `tables/`。
5. `chunk_id` 由 doc_id、标题路径哈希、起始页、文档内序号与正文哈希确定性生成，相同输入重复运行结果一致；正文变化会改变 ID。
6. CLI 命令记录同时包含顶层命令与「子命令」小节条目，二者通过 `parent_command_id` 关联。
7. 极短 chunk：正文少于 20 字的 chunk 共 4 条，全部来自文档本身即为极短的小节（如 API 手册「1.6 注意事项」正文只有「暂无」），按规范原样保留、未做人为扩写。
8. 产品手册的「术语名：说明文字」条目与真标题使用同一字体（SourceHanSansSC-Medium）、同一字号（10.5pt）、同一字重，无法靠版式区分。现按「冒号后是否跟随含句读标点的成句说明」识别正文条目，避免其被误判为标题、导致续行沦为孤立碎片；经比对目录，未误伤任何真标题。

## 12. 复现方式

```bash
python knowledge/scripts/build_knowledge.py
python knowledge/scripts/validate_processed.py
python knowledge/scripts/build_knowledge.py --stage report
```

