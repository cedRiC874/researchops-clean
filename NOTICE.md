# 复用代码说明

来源项目：[cedRiC874/researchops-agent](https://github.com/cedRiC874/researchops-agent)。
来源项目和本项目均由 cedRiC874 编写并使用 MIT 许可；许可全文见 [LICENSE](LICENSE)。

下表每一项均取自提交 `61676469a1be721b6ac6dfdcb3a1bf7fc04207eb`。
代码从该提交只读提取，未改动旧仓库。

| 本项目文件 | 来源路径或符号 |
| --- | --- |
| `src/researchops_clean/ledger.py` | `src/researchops/audit.py` |
| `tests/test_ledger.py` | `tests/test_audit.py`、`tests/test_audit_chain_rows.py` |
| `src/researchops_clean/artifact_security.py` | `src/researchops/artifact_security.py` |
| `scripts/run_offline_tests.py` | `services/expert_review_v1/scripts/run_offline_tests.py` |
| `src/researchops_clean/atomic_io.py` | `services/expert_review_v1/src/researchops_expert_review/core.py` 的 `_write_json` |
| `src/researchops_clean/sensitive_patterns.py` | 同一 `core.py` 的 `_PATTERNS` |
| `src/researchops_clean/paths.py` | `src/researchops/self_pilot.py` 的 `_validate_session_directory` |
| `.gitattributes` | `.gitattributes` 的通用规则 |
| `LICENSE` | `LICENSE`，原样保留版权与 MIT 许可 |
| `requirements.lock` | `requirements.lock` 中 jsonschema 及其传递依赖的锁定版本 |

## 删减与调整

`ledger.py` 保留通用运行、事件、工具调用、批准、尝试、模型调用记录、哈希链校验与导出功能。删除：

- `_CompletionTelemetryWriteCapability`、`_CompletionTransportSendCapability`。
- `_create_completion_telemetry_write_capability`、`append_first_live_transport_send_event`、
  `_create_transport_send_capability`、`append_transport_send_event`、
  `append_completion_telemetry_event`、`append_timed_completion_telemetry_event`、
  `_append_validated_completion_terminal_or_start`。
- 仅供上述代码使用的 `_completion_exact_mapping`、`_completion_index`、
  `_validated_completion_runtime_binding`、`_validated_completion_event_payload`、
  `_validate_completion_capability_identity`、`_validate_completion_event_transition`。
- 专用常量 `COMPLETION_TELEMETRY_EVENT_SCHEMA_VERSION`、`_COMPLETION_SEND_FIELDS`、
  `_COMPLETION_SEND_TOKEN`、`_COMPLETION_TERMINAL_KIND_BY_EVENT`、
  `_COMPLETION_SAFE_ID`、`_COMPLETION_SAFE_ERROR`、`_COMPLETION_SHA256`、
  `_COMPLETION_BINDING_FIELDS`、`_COMPLETION_STARTED_FIELDS`、
  `_COMPLETION_TERMINAL_FIELDS`、`_COMPLETION_ACCEPTED_FIELDS`、
  `_COMPLETION_WRITE_CAPABILITY_TOKEN`，以及 `threading` 导入。

所有保留函数和方法的逻辑保持不变。`_append_event_tx` 的保留事件类型检查及其引用的常量原样保留；为保持 `__init__` 原样，其中未再使用的 `_completion_capability_token` 初始化也保留。

其他调整：

- `artifact_security.py` 原样拷贝。
- `_write_json` 重命名为 `write_json_atomic`，函数体原样保留。
- `_PATTERNS` 仅保留 `identity_number`、`phone_number`、`email`、`file_path`、`api_key` 及其引用的 `_ASCII_PATH_PART`、`_RELATIVE_PATH_START` 常量；删除旧项目专用的 `subject_id` 模式。
- `_validate_session_directory` 重命名为 `validate_output_directory`，保持目录判断逻辑，异常类型改为本项目的 `OutputDirectoryError`。
- 离线测试入口保留原 socket 和 DNS audit hook、JSON 报告，删除旧仓库的 root/service 范围选择及服务测试发现分支，改为只运行本仓库 `tests/`，并自行将 `src/` 加入导入路径。
- `.gitattributes` 仅保留通用规则，删除所有旧项目专用的冻结目录、证据快照和脚本规则。
- 依赖仅保留 jsonschema 及其传递依赖；安装验证发现 `referencing` 还需要 `typing_extensions`，补锁为 `4.16.0`，与来源仓库锁定版本一致。

## 测试来源

`tests/test_audit.py` 的 6 个测试和 `tests/test_audit_chain_rows.py` 的 2 个测试全部保留，合并到 `tests/test_ledger.py`，仅调整导入路径和合并文件所需的布局，未改动断言。**删除的原测试：无。** 其中保留事件类型拒绝测试仍适用于保留的账本防护。

新增测试放在独立测试文件中，覆盖工具调用完整流程、篡改检测、导出脱敏、原子写入及失败清理、敏感模式、输出目录限制和平台权限行为，全部只使用合成数据。
