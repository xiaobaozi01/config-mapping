# GNS3 配置自适应

将 IOS XR 和 Junos 大括号配置转换为适用于 GNS3 XRv9000/vMX 的配置。第一次接触项目请从 [新人导读](docs/onboarding-guide.md) 开始；需求基线见 [docs/requirements.md](docs/requirements.md)，完整转换过程见 [docs/processing-flow.md](docs/processing-flow.md)，新增 group 语义规则见 [语义 identity 规则编写指南](docs/semantic-rule-authoring.md)。

拓扑预检后会展开常见的 IOS XR `group/apply-group` 和 Junos `groups/apply-groups` 配置，使继承的接口及认证配置进入后续转换。已应用 group 无法静态求值时转换失败，不输出可能不完整的设备配置。

group 展开是事务式的，并遵循显式配置、嵌套层级和 group 列表顺序的厂商优先级。冲突详情写入 `report.json` 的 `group_conflicts` 字段。

## 代码边界

- `cisco/`：IOS XR AST、解析/渲染、Group、接口、清洗、模拟适配及 XRv9000 规则。
- `juniper/`：Junos AST、解析/渲染、Group、接口、清洗、模拟适配及 vMX 规则。
- `common/`：两家厂商共享的接口类型、操作结果、策略、不变量和语义 identity 引擎。
- `common/contracts.py`：Cisco 与 Juniper 显式实现的稳定厂商能力协议，厂商 AST 不向流程层泄漏。
- `adaptation/topology.py`：Excel 拓扑读取、预检和输出。
- `adaptation/groups.py`：Group 展开的跨设备调度和诊断汇总。
- `adaptation/interfaces.py`：接口分类及最终引用改写。
- `adaptation/nni.py`：NNI 聚合规划、M-LAG 边界、端口分配和配置迁移。
- `adaptation/uni.py`：UNI 候选识别、VLAN 分配和 QinQ 汇聚。
- `adaptation/cleaning_rules.py`：声明式与代码型清洗规则、内置注册表及固定执行点调度。
- `adaptation/simulation.py`：模拟参数适配调度。
- `adaptation/pipeline.py`：显式组合转换阶段，并在首次错误后停止。
- `adaptation/service.py`：组织输入加载、完整转换和最终输出。

生产代码只保留以上四个业务目录，不再维护旧目录或旧导入路径。

## 测试数据

单元测试按关注点拆分到独立的 `tests/test_*.py` 文件（端到端转换、Cisco/Junos group 展开、AST、清洗、模拟适配与接口业务分析），设备配置一律以 `.cfg` 文件放在 `tests/fixtures/`，拓扑以双 sheet 的 `topology.xlsx` 表达，不嵌入 Python 代码：

- `iosxr_bundle/`：IOS XR 聚合 NNI、UNI、group 和认证清洗样例。
- `junos_bundle/`：Junos 聚合 NNI、UNI、groups 和认证清洗样例。
- `iosxr_duplicate_vlan/`：UNI VLAN 冲突重新分配样例。
- `iosxr_mlag/`：IOS XR 同一 Bundle 跨对端拆分、一对多引用与 QinQ UNI 样例。
- `junos_mlag/`：Junos ae 跨对端拆分、裸口清理与 QinQ UNI 样例。
- `cross_vendor/`：Cisco XRv9000 与 Juniper vMX 互联时，普通 NNI 和 `Bundle-Ether`/`ae` 聚合两端独立适配样例。
- `junos_unresolved/`：无法解析 group 时事务回滚样例。
- `group_configs/`：独立的厂商 group 优先级、通配、`inactive`/`protect`、引号组名与 `apply-groups-except` 配置。
- `washing_configs/`：管理面必清项、默认保留项及五类可选清洗开关样例。
- `simulation_adaptation/`：目标镜像模拟参数适配与幂等性样例。
- `cisco_ast/`：IOS XR 持久 AST 层级、`!` 边界与编辑样例。
- `junos_ast/`：Junos 行内块、注释、hostname 与括号边界样例。
- `semantic_identity/`：规则 identity 覆盖关系与未知语句降级样例。
- `interfaces/`：接口识别、业务闭包分析与 BVI/VLAN 推断样例。

每个端到端样例目录包含一个双 sheet 的 `topology.xlsx` 和对应的 `configs/*.cfg`。

## 安装与运行

```bash
python3 -m pip install -e .
config-adaptor convert \
  --topology topology.xlsx \
  --config-dir configs \
  --output-dir output \
  --profiles config/image_profiles.yaml
```

`--profiles` 可配置镜像名称/版本、数据接口列表和模拟参数适配策略；列表最后一个接口用于 UNI，其余接口可分配给 NNI。清洗规则随程序发布，按厂商分别位于 `config_adaptor/cisco/rules/xrv9000/cleaning.yaml` 和 `config_adaptor/juniper/rules/vmx/cleaning.yaml`，转换时不接受外部规则文件。

每个镜像 Profile 的 `simulation_adaptation.mode` 支持 `off`、`compatible` 和 `stable`。默认 `stable` 仅保证已映射数据口启用、删除这些目标口上的物理硬件属性，并将配置中已经存在且低于安全下限的 BFD interval/multiplier 调高；不会自动开启 BFD，也不会修改 OSPF、IS-IS 或 BGP 的业务定时器。全部修改写入 `report.json` 的 `simulation-adaptation` 事件。旧 `param_adjustment` YAML 节点仍兼容读取，但不能与新节点同时配置。

管理账号、AAA/TACACS/RADIUS、SSH/Telnet 网络管理服务、SSH 信任、SNMP 以及聚合扁平化后的 LACP/门限属性始终清理。简单清洗由 YAML 描述，banner、认证替换等复杂清洗由代码规则实现，两类规则统一由 `CleaningRulesHandler` 在固定执行点调度并按 `category` 报告。协议认证等可能改变业务能力的 YAML 规则默认关闭。

group 统一采用全量处理：静态展开并校验所有活动的 group 引用及其依赖，成功后移除 group 定义和控制语句。未被活动配置引用的模板不参与求值；缺失引用、循环依赖或无法安全静态解释的活动 group 会使转换整体回滚。Junos 中所有 `inactive:` 节点及其完整子树都会删除；重复的顶层 `groups {}` 容器及同名定义会先合并再求值。

group 叶子语句使用 XRv9000/vMX 路径感知规则判定语义冲突。未匹配规则且同路径同命令族出现不同值时，固定按 `warn` 处理：保留语句并报告潜在歧义。报告中的 `group-identity-coverage` 事件和 summary 字段给出规则命中数、降级数及实际覆盖率。

Cisco 与 Juniper 的跨厂商 NNI 会按链路两端各自的厂商语法和镜像 Profile 独立分配接口，不要求两端目标接口同名。转换器不审计链路两端的 IP、VLAN、MTU 或封装是否一致，这些业务一致性由输入配置保证。

UNI 只迁移有 IP、L2VC/L2Circuit、L2 绑定或全局业务引用的接口；裸口删除。Cisco 会沿 `bridge-domain` 关联活跃 attachment circuit 与 BVI，Juniper 会沿 `bridge-domains`/`vlans` 关联活跃接口、VLAN 与 IRB；未关联活跃业务的 BVI/IRB 不迁移。目标 UNI 统一重写为 QinQ，旧标签匹配和 VLAN rewrite 清除，外层 VLAN 是设备内唯一的目标子接口编号，内层优先保留可识别的原 VLAN。

也可以不安装直接运行：

```bash
PYTHONPATH=src python3 -m config_adaptor convert --topology topology.xlsx --config-dir configs --output-dir output
```

固定实验账号为 `labadmin / Gns3Lab@2026`，仅用于隔离的 GNS3 实验环境。

IOS XR 认证清洗同时删除自定义 `usergroup`、`taskgroup` 以及 `line` 下对旧认证、授权、计费和用户组的引用；`exec-timeout` 等非认证终端参数保留。删除结果写入 `cisco.replace.authentication` 清洗事件的 `removed_by_type`。

XRv9000 7.11.1 的默认适配会识别 IOS XR `interface preconfigure`，并在接口分类前通过 `active=False` 忽略这些尚未实例化的接口候选配置。默认启用的 `cisco.remove.ptp.interface` 规则会在接口迁移和引用改写完成后停用实验不需要的 `interface PTP...` 虚拟接口块。

## 默认 Excel 列

设备列表：`设备名称`、`厂商`、`配置文件`。链接表：`A端设备`、`A端接口`、`Z端设备`、`Z端接口`。程序同时识别常见中英文别名。

## 输出

- `topology-adapted.xlsx`
- `configs/*.cfg`
- `interface-mapping.json`
- `report.json`
- `README.txt`
