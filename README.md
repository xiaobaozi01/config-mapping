# GNS3 配置自适应

将 IOS XR 和 Junos 大括号配置转换为适用于 GNS3 XRv9000/vMX 的配置。第一次接触项目请从 [新人导读](docs/onboarding-guide.md) 开始；需求基线见 [docs/requirements.md](docs/requirements.md)，完整转换过程见 [docs/processing-flow.md](docs/processing-flow.md)，新增 group 语义规则见 [语义 identity 规则编写指南](docs/semantic-rule-authoring.md)。

拓扑预检后会展开常见的 IOS XR `group/apply-group` 和 Junos `groups/apply-groups` 配置，使继承的接口及认证配置进入后续转换。已应用 group 无法静态求值时转换失败，不输出可能不完整的设备配置。

group 展开是事务式的，并遵循显式配置、嵌套层级和 group 列表顺序的厂商优先级。冲突详情写入 `report.json` 的 `group_conflicts` 字段。

## 厂商实现边界

- `vendor/api.py`：应用层依赖的稳定厂商配置协议；厂商 AST 不向流程层泄漏。
- `parsers/cisco_iosxr.py`：保留 IOS XR 语法模型、解析/渲染和兼容门面。
- `parsers/juniper_junos.py`：保留 Junos 大括号语法模型、解析/渲染和兼容门面。
- `vendor/*/groups.py`：分别封装事务式 Group 展开、继承优先级和冲突检测。
- `vendor/identity/`：加载、校验并匹配路径感知的配置语义规则。
- `vendor/cisco/rules/xrv9000/`、`vendor/juniper/rules/vmx/`：随代码发布的镜像专属 identity 规则包。
- `vendor/*/interfaces.py`：分别封装接口识别、业务闭包分析及 NNI/UNI 接口树修改。
- `vendor/*/cleaning.py`：分别封装 Cisco/Junos 的管理面与可选能力清洗。
- `vendor/*/simulation.py`：分别封装目标镜像参数适配。
- `parsers/common.py`：只放接口数据结构以及父接口、子接口编号等无厂商语义的工具。
- `application/nni/planner.py`：纯计算聚合链路分组与 M-LAG 边界，不修改配置或拓扑。
- `application/uni/vlan_allocator.py`：封装 UNI VLAN 唯一性和冲突分配规则。
- `application/pipeline.py`：显式组合转换阶段，并在首次错误后停止。
- `handlers.py`：保留拓扑预检、Group、NNI、UNI、引用更新、清洗和模拟适配阶段，只通过统一厂商接口调度。

`documents.py` 仅作为旧导入路径的兼容门面；新增厂商逻辑应放入各自模块。

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
  --profiles config/image_profiles.yaml \
  --washing-policy config/washing_policy.example.yaml
```

`--profiles` 可配置镜像名称/版本、数据接口列表和模拟参数适配策略；列表最后一个接口用于 UNI，其余接口可分配给 NNI。需要增加保守清洗规则时传入 `--rules config/cleaning_rules.example.yaml`。

每个镜像 Profile 的 `simulation_adaptation.mode` 支持 `off`、`compatible` 和 `stable`。默认 `stable` 仅保证已映射数据口启用、删除这些目标口上的物理硬件属性，并将配置中已经存在且低于安全下限的 BFD interval/multiplier 调高；不会自动开启 BFD，也不会修改 OSPF、IS-IS 或 BGP 的业务定时器。全部修改写入 `report.json` 的 `simulation-adaptation` 事件。旧 `param_adjustment` YAML 节点仍兼容读取，但不能与新节点同时配置。

管理账号、AAA/TACACS/RADIUS、SSH/Telnet 网络管理服务、SSH 信任、SNMP 以及聚合扁平化后的 LACP/门限属性始终清理。协议认证、PKI、硬件、NAT 和流量统计由独立的可选能力清洗阶段处理；`--washing-policy` 中的对应开关只有显式改为 `true` 才生效。

group 统一采用全量处理：静态展开并校验所有活动的 group 引用及其依赖，成功后移除 group 定义和控制语句。未被活动配置引用的模板不参与求值；缺失引用、循环依赖或无法安全静态解释的活动 group 会使转换整体回滚。Junos 中所有 `inactive:` 节点及其完整子树都会删除；重复的顶层 `groups {}` 容器及同名定义会先合并再求值。

group 叶子语句使用 XRv9000/vMX 路径感知规则判定语义冲突。未匹配规则且同路径同命令族出现不同值时，`group_handling.unknown_identity` 可设为 `preserve`、`warn`（默认）或 `fail`。报告中的 `group-identity-coverage` 事件和 summary 字段给出规则命中数、降级数及实际覆盖率。

Cisco 与 Juniper 的跨厂商 NNI 会按链路两端各自的厂商语法和镜像 Profile 独立分配接口，不要求两端目标接口同名。转换器不审计链路两端的 IP、VLAN、MTU 或封装是否一致，这些业务一致性由输入配置保证。

UNI 只迁移有 IP、L2VC/L2Circuit、L2 绑定或全局业务引用的接口；裸口删除。Cisco 会沿 `bridge-domain` 关联活跃 attachment circuit 与 BVI，Juniper 会沿 `bridge-domains`/`vlans` 关联活跃接口、VLAN 与 IRB；未关联活跃业务的 BVI/IRB 不迁移。目标 UNI 统一重写为 QinQ，旧标签匹配和 VLAN rewrite 清除，外层 VLAN 是设备内唯一的目标子接口编号，内层优先保留可识别的原 VLAN。

也可以不安装直接运行：

```bash
PYTHONPATH=src python3 -m config_adaptor convert --topology topology.xlsx --config-dir configs --output-dir output
```

固定实验账号为 `labadmin / Gns3Lab@2026`，仅用于隔离的 GNS3 实验环境。

IOS XR 认证清洗同时删除自定义 `usergroup`、`taskgroup` 以及 `line` 下对旧认证、授权、计费和用户组的引用；`exec-timeout` 等非认证终端参数保留。删除结果按类别写入 `report.json` 认证事件的 `removed_by_type`。

## 默认 Excel 列

设备列表：`设备名称`、`厂商`、`配置文件`。链接表：`A端设备`、`A端接口`、`Z端设备`、`Z端接口`。程序同时识别常见中英文别名。

## 输出

- `topology-adapted.xlsx`
- `configs/*.cfg`
- `interface-mapping.json`
- `report.json`
- `README.txt`
