# researchops-clean

面向医院内网离线运行的医疗数据清洗 agent：模型提出清洗规则，由人批准后交给固定程序执行，并为每一步保留可校验、防篡改的审计记录。

当前处于早期开发阶段，已包含基础模块、离线配方执行层和测试，不调用模型。**只使用合成数据，不要放入真实病人数据。**

设计原则：

- 数据和模型都在院内。
- 模型只看完成任务所需的最少内容。
- 模型不能直接改数据，只能提出规则。
- 每处改动都要人批准，并且可追溯、可撤销。

本项目从同一作者的 MIT 项目 [researchops-agent](https://github.com/cedRiC874/researchops-agent) 复用通用基础模块；来源提交和删减记录见 [NOTICE.md](NOTICE.md)。

使用 Python 3.11 或更高版本。在仓库根目录安装锁定依赖，然后运行测试：

```sh
python -m pip install -r requirements.lock
python scripts/run_offline_tests.py --report artifacts/offline-tests.json
```

测试使用标准库 `unittest`，运行时禁止外部 socket 连接和 DNS 解析，结果写入 JSON 报告。依赖安装需要可用的包源；部署到离线环境前须提前准备锁定版本的依赖。

## 离线配方执行层

现在已提供确定性的配方执行、逐项变更记录和重放核验。执行层不联网、不调用模型、不读取密钥或环境变量的值；所有 CSV 单元格均按字符串处理，不删除行，也不填补缺失值。本阶段只使用手写的合成数据。

安装锁定依赖后，在仓库根目录安装本地命令行入口：

```sh
python -m pip install --no-deps --no-build-isolation -e .
```

也可直接使用 `src`，将下文每条命令的 `python -m researchops_clean` 替换为
`python -c "import runpy,sys;sys.path.insert(0,'src');runpy.run_module('researchops_clean',run_name='__main__')"`。
离线安装前需准备好依赖和构建工具；运行以下命令本身不需要网络。

```sh
python -m researchops_clean ops
python -m researchops_clean approve --recipe draft.json --steps S1,S2 --reviewer reviewer-a --output approved.json
python -m researchops_clean apply --input data.csv --recipe approved.json --output-dir artifacts/cleaning/run-01 --encoding utf-8-sig
python -m researchops_clean verify --input data.csv --recipe approved.json --run-dir artifacts/cleaning/run-01
```

`ops` 输出 9 个操作的名称、版本及完整参数 schema。`approve` 仅批准指定步骤，写入审核人、UTC 时间，另存新文件并打印新配方的规范化 JSON 哈希；目标已存在时拒绝覆盖。审核人须为 1–40 个字符，不能包含路径分隔符或控制字符。批准是人工命令行操作，没有网页、HTTP 或 MCP 批准接口。

配方使用 `cleaning-recipe/1` 格式，绑定输入文件原始字节的 SHA-256 及完整、有序的表头。待批准步骤的参数允许暂不完整，批准时校验所选步骤，执行时校验全部步骤。只有全部步骤为 `approved` 才能执行；**被 `rejected` 的步骤必须先从配方中移除**。未知操作、非法参数、缺少列、输入哈希或表头不符都会拒绝执行。

每次 `approve` 会在所选步骤的批准记录中写入 `approval.content_sha256`，绑定 `input_sha256`、`step_order` 和该步骤的 `step_id`、`op`、`params`。`step_order` 是配方全部步骤编号的有序列表，包括仍为 `pending` 的步骤；`reason` 不参与绑定。`apply` 和 `verify` 均重新校验，若批准哈希缺失或不一致，会以退出码 `2`、错误码 `approval_content_mismatch` 拒绝，并列出相关 `step_id`。旧批准记录，包括此前未绑定顺序的四字段哈希，都须重新运行 `approve`，不会自动兼容。这是内容一致性校验，不是审核人身份的数字签名。

先删除不需要执行的步骤，再批准保留的步骤。批准后若增加、删除、重排步骤或修改步骤编号，所有已批准步骤都须重新批准；步骤编号列表不变时，可以分批批准。修改输入哈希或某个已批准步骤自身的 `op`、`params` 后，也须重新批准受影响的步骤。

仅修改 `reason` 不会使该步骤的批准失效，仍可用于新的 `apply`。已有运行的 `verify` 继续校验整个配方的 `recipe_hash`，因此不能用修改过理由的配方核验旧运行。

批准内容哈希保留 Unicode 原文：将上述五个字段按键排序、紧凑 JSON 格式序列化为 UTF-8 后计算 SHA-256，不做 Unicode 归一化。这里不使用会进行 NFC 归一化的 `ledger.sha256_json`，以免原文不同的列名或映射字符串共用同一批准哈希；账本的既有哈希规则不变。CSV 读取不会修改列名；若任意两个列名经 NFC 归一化后相同，会以 `ambiguous_column_names` 拒绝读取，须先人工消除歧义。重复表头的错误提示只报告列位置，不回显列名。

新派生列也按 NFC 检查是否与已有列或其他新派生列冲突：批准阶段以 `recipe_invalid` 拒绝，模型建议以 `output_column_conflict` 拒绝。对已有列的引用仍须精确匹配原列名，不会自动归一化。

`apply` 以当前工作目录为项目根，输出目录必须是 `artifacts/` 下尚不存在的独立子目录。输入限制为 50 MB、1,000,000 行，表头不允许空列名或重复列名。编码只允许 `utf-8-sig`（默认，也兼容普通 UTF-8）、`utf-8`、`gbk` 和 `gb18030`；`verify` 按 manifest 中记录的输入编码重放。

| 操作（版本均为 `1`） | 参数 | 行为 |
|---|---|---|
| `normalize_text` | `column` | 去掉首尾空白，将全角 ASCII 转半角；重复执行结果不变 |
| `sentinel_to_missing` | `column`, `values`（字符串列表） | 完全匹配的哨兵值改为空并标记 `sentinel_missing` |
| `map_values` | `column`, `mapping`（非空映射）, `on_unmapped`（`keep` / `keep_and_flag` / `error`） | 按完全相等映射，跳过空值 |
| `parse_date` | `column`, `output_column`, `formats`, `output`（`date` / `datetime`）, `excel_serial`（`enabled`, `min`, `max`） | 写入新日期列；歧义或不能解析的日期留空并标记 |
| `parse_age_cn` | `column`, `outputs`（`years`, `months`, `days`） | 提取岁、月、天三个整数分量，不作单位换算 |
| `parse_lab_result` | `column`, `qualitative_values`, `invalid_values`, `outputs`（`value`, `qualifier`, `qualitative`, `invalid`） | 分离数值、限定符、定性值及无效状态 |
| `convert_unit` | `value_column`, `unit_column`, `conversions`（`from`, `to`, `factor`），可选 `where` | 精确十进制换算；未知、缺失单位或非数值只加标记 |
| `range_check` | `column`, `min`, `max`，可选 `where` | 闭区间检查，超出范围只加标记 |
| `flag_duplicates` | `key_columns` | 按当前值标记重复组内所有行的各个键单元格，组号写入 detail，不删行 |

`factor`、`min`、`max` 均为十进制字符串；`parse_date.excel_serial.min/max` 则为整数。
`where` 可写为 `{"column":"项目","equals":"血红蛋白"}` 或 `{"column":"项目","in":["血红蛋白","Hb"]}`。
仅接受 ASCII 十进制数，不接受科学计数法、`NaN`、`Infinity` 或未归一化的全角数字。解析类操作保留原列，将新列按步骤顺序追加到表尾；新列不得重名。

## 输出与核验

全部输出先写入临时目录，成功后整体发布。失败不留下半成品，也不覆盖已有输出目录。CSV 统一为 UTF-8（无 BOM）、LF 换行，原列保留原顺序。

| 文件 | 内容 |
|---|---|
| `cleaned.csv` | 清洗结果及新增解析列 |
| `changes.csv` | `row,column,old_value,new_value,step_id,op`；数据行号从 1 开始，不含表头 |
| `flags.csv` | `row,column,flag_code,step_id,detail` |
| `run_manifest.json` | 输入文件名、哈希、编码、行数和表头，配方哈希、步骤数，输出哈希、各步骤版本与计数，运行环境、警告、UTC 时间和确定性 `run_id` |

manifest 只记录输入文件名，不包含本机绝对路径。以 `=`、`+`、`-`、`@` 开头且非纯数值的单元格会计入警告，不修改取值。
每一步的 `changed_cells`、`flagged_cells` 分别按唯一的 `(row,column)` 统计改动单元格和标记单元格。
`run_id` 为 `C-` 加 12 位大写十六进制哈希，不受运行时间和 Python 版本影响；同一输入与配方的三个 CSV 和 `run_id` 应一致。

| 命令 | 退出码 `0` | 退出码 `1` | 退出码 `2` |
|---|---|---|---|
| `ops` / `approve` | 成功 | 不使用 | 参数或配方无效、目标已存在等拒绝 |
| `apply` | 执行成功 | 不使用 | 拒绝执行 |
| `verify` | 文件完整且重放的三个 CSV 逐字节一致 | 文件完整，但重放结果不同 | 文件缺失或被改动，或输入、配方哈希与 manifest 不符等 |

命令输出 JSON；拒绝执行时报告错误码和中文说明，例如 `recipe_step_not_approved`、`input_hash_mismatch`、`recipe_invalid`、`column_missing`、`output_exists` 和 `output_path_not_allowed`。

**真实数据场景下，`changes.csv`、`flags.csv` 含原始取值，只能留在本机，不能提交到仓库，也不能发送给任何外部服务。** 当前示例和测试全部使用手写的合成数据，不下载数据集。

验收示例位于 `tests/fixtures/lab_example.csv`，配套的 `tests/fixtures/lab_recipe.json` 已批准全部 11 步。可将示例命令中的 `data.csv` 和 `approved.json` 分别替换为这两个路径直接执行；再次运行时须使用新的输出目录。配方只统一血红蛋白别名，`CRP` 和 `超敏C反应蛋白` 保持分开。示例的范围界限 `30`–`250` 仅用于合成数据，真实界限由医生确定。

## 清洗剖析与规则式问题清单

在人工批准配方之前，可以先查看每列的类型、缺失情况、格式模式和清洗线索，再由固定规则生成问题卡片与配方草稿。此流程仍然离线，不调用模型。

```sh
python -m researchops_clean profile --input data.csv --output-dir artifacts/cleaning/profile-01
python -m researchops_clean propose --input data.csv --output-dir artifacts/cleaning/propose-01
```

两个命令均支持 `--k`（默认 `5`，不能小于 `5`）及与 `apply` 相同的 `--encoding`。输出目录规则也与 `apply` 相同：只能写入 `artifacts/` 下尚不存在的独立子目录，先写临时目录再整体发布，不覆盖已有目录。成功退出码为 `0`，拒绝时为 `2`，命令行输出计数摘要或中文错误 JSON，不输出绝对路径。

| 文件 | 生成命令 | 用途 |
|---|---|---|
| `internal_profile.json` | `profile` / `propose` | 留给本机人工查看，可以包含具体取值 |
| `model_profile.json` | `profile` / `propose` | 将来模型唯一可以读取的剖析结果 |
| `issues.json` | `propose` | 符合性、完整性、合理性问题卡片及待人工决定事项 |
| `draft_recipe.json` | `propose` | 绑定输入哈希与表头、所有步骤均为 `pending` 的配方草稿 |

`model_profile.json` 不包含整行数据、路径、最小值、最大值或分位数。每列提供列名、kind、计数和格式模式；只允许列出 `code_like` 列的取值及 `numeric_with_tokens` 列的非数字取值，且出现次数必须至少为 `k`。被隐藏的低频或敏感值合并为“其他 N 个值，共 M 次”，不展示取值本身。`identifier_like` 列不展示任何取值；命中身份证号、手机号或邮箱格式的值在任何列中都不展示。可能复现被隐藏原值或敏感格式的模式也会被隐藏。列名自身含敏感格式或路径时，剖析会拒绝执行，须先在本机修改列名。内部剖析不可作为模型输入。

模型剖析另保留输入 SHA-256、表头、行数和完全重复行的聚合计数，用于绑定草稿；不提供重复行内容或具体键值。

问题卡片的证据仅来自 `model_profile.json`。规则不会生成 `range_check`，数值界限仍须由医生提供。标识符列只提示将在去标识化阶段处理，不生成该列的转换步骤；完全重复行检查仍按全部原列作键。

**草稿必须由人核对、补全并用 `approve` 批准后才能 `apply`。** 代码值候选须逐个确认是否应当转为缺失；定性或无效词表之外的检验结果须人工决定。`map_values` 草稿的目标值会留空，必须先填写；空映射或任何空目标值都会被 `approve` 和 `apply` 拒绝。若确认某个值应转为缺失，应使用 `sentinel_to_missing`。

若隐私过滤使具体候选值不可见，草稿会保留待填写的参数，卡片说明需要人工补充；不能从格式模式猜测原值。人工可在本机查阅 `internal_profile.json`，补全后再批准。

`map_values` 只认映射里写明的键，已经是规范写法的值也要写入映射，例如 `"男": "男"`；否则在草稿使用的 `keep_and_flag` 策略下仍会被标为未映射。填写后，按问题卡片中的步骤编号逐项审核，再沿用上文的 `approve`、`apply`、`verify` 命令。

支持的平台为 **Windows 和 Linux**。在 macOS 上，`apply` 会因为缺少“禁止覆盖的原子改名”而拒绝执行。

## 本机模型提案

`model-propose` 在规则式草稿后追加模型建议。它先从输入 CSV 在内存中重新生成隐私过滤后的剖析和问题卡片，不读取磁盘上已有或人工修改过的剖析、草稿。原有六个命令继续独立运行；只有显式运行 `model-propose` 才会发送模型请求。

以下为将来连接本机服务的用法，将 `local-model` 替换为实际模型名。Ollama 示例：

```sh
python -m researchops_clean model-propose --input data.csv --endpoint http://127.0.0.1:11434/v1 --model local-model --output-dir artifacts/cleaning/model-ollama-01
```

vLLM 示例：

```sh
python -m researchops_clean model-propose --input data.csv --endpoint http://127.0.0.1:8000/v1 --model local-model --output-dir artifacts/cleaning/model-vllm-01
```

**本版本只允许本机地址 `127.0.0.1`、`::1` 或 `localhost`，不支持 Key，请勿指向任何在线 API 或内网服务器。** 仅接受 HTTP 或 HTTPS，地址不能包含用户名、密码、查询串或片段。请求忽略系统代理，不带 Authorization 头，不跟随跳转，只发送一次且不重试。

可选参数如下：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--encoding` | `utf-8-sig` | 与 `apply` 相同的四种输入编码 |
| `--timeout` | `120` | 请求超时秒数，最大 `600` |
| `--max-tokens` | `4096` | 响应 token 上限 |
| `--response-format` | `json_schema` | 可改为 `none`，省略请求中的结构化输出约束 |

建议使用支持结构化输出的服务，并关闭模型的“思考模式”。无论是否发送 schema，回答都必须是完整的 JSON 对象；夹杂思考文字、Markdown 代码围栏或其他说明都会被拒绝。

模型只看到 `model_profile`、规则式问题卡片，以及除 `range_check` 以外的 8 个操作的名称、版本和参数 schema。内部剖析、整行数据、输入文件名和本机路径都不发送；请求上限为 256 KiB，响应上限为 1 MiB。模型不能提出数值界限、删除行或填补缺失的方案。

发送前的隐私检查按本次授权调整为：解码请求中的每个 JSON 键和字符串值后，先检查单个字段长度；超过 2,000 个字符时，以 `request_field_too_long` 拒绝发送并返回退出码 `2`。长度为 2,000 或以下时，照常完整调用现有 `find_sensitive_types`，固定说明也接受同样检查；原有敏感正则不变。`user.content` 作为 JSON 容器先解码，再逐个检查其中的键和字符串值，不把整个容器算作一个字段。只有问题卡片的 `evidence.pointer` 可以豁免 `file_path`，且必须能在本次 `model_profile` 中解析，并与对应 `evidence.value` 完全一致。该字段的其余四类敏感检测仍然执行；不满足条件时按普通字符串完整扫描。

模型最多提出 30 条建议。每条建议分别校验操作、参数、列引用、取值来源和理由；与已有步骤完全相同的建议只记为重复。固定词表中的值若未出现在该列公开取值中，会附上 `value_unverified` 警告，须人在本机核对。接受的步骤编号为 `M1`、`M2` 等，理由以“【模型建议】”开头，**所有规则步骤和模型步骤仍为 `pending`，必须人工补全、审核并通过 `approve` 才能执行**。

输出目录与 `propose` 使用相同规则：在 `artifacts/` 下先写临时目录，全部完成后整体发布，不覆盖已有目录。

| 文件 | 内容 |
|---|---|
| `model_profile.json`、`issues.json` | 本次重新生成的模型剖析和规则问题卡片 |
| `model_request.json` | 实际发送的请求正文 |
| `model_response.json` 或 `model_response.txt` | 收到的响应正文；无响应时不生成 |
| `model_proposals.json` | 接受、拒绝、重复的建议及警告、原因 |
| `draft_recipe.json` | 规则步骤加接受的模型步骤，仅成功时生成 |
| `run_record.json` | 本机地址的主机和端口、模型、请求参数、用量、耗时、计数、文件哈希、状态及账本核验结果 |
| `ledger.sqlite3` | 一次运行和模型调用的审计记录，不记提示词或回答原文 |

账本关闭且 `verify_chain` 通过后才发布，输出不遗留 `-wal`、`-shm` 文件。连接、超时、HTTP 或模型回答失败时，也会保留请求、已有响应、失败记录和账本，便于本机核查，但不生成可继续审批的草稿。

若发送后本地保存或发布本身失败，退出码仍为 `3`，错误说明会指出未能完成保存。

| 退出码 | 含义 |
|---|---|
| `0` | 提案流程成功；允许有建议被拒绝或判为重复 |
| `2` | 发送前拒绝，例如参数、地址、输入、隐私检查或输出目录不符合要求 |
| `3` | 请求失败、回答不可用或发送后保存失败；常规调用失败会保存证据 |

**测试和 CI 从不启动或调用真实模型。** 所有涉及 HTTP 的测试只连接测试自己在 `127.0.0.1` 随机端口启动的假服务，使用手写合成输入和预设响应；本次实现和验收也不调用 Ollama、vLLM 或在线 API。
