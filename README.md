# researchops-clean

面向医院内网离线运行的医疗数据清洗 agent：模型提出清洗规则，由人批准后交给固定程序执行，并为每一步保留可校验、防篡改的审计记录。

当前处于早期开发阶段，仅包含基础模块和测试，尚未实现清洗功能，也不调用模型。**只使用合成数据，不要放入真实病人数据。**

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
