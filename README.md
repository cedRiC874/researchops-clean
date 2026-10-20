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
