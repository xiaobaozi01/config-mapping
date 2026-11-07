# GNS3 配置自适应

将 IOS XR 和 Junos 大括号配置转换为适用于 GNS3 XRv9000/vMX 的配置。第一次接触项目请从 [新人导读](docs/onboarding-guide.md) 开始；需求基线见 [docs/requirements.md](docs/requirements.md)，完整转换过程见 [docs/processing-flow.md](docs/processing-flow.md)，新增 group 语义规则见 [语义 identity 规则编写指南](docs/semantic-rule-authoring.md)。裁剪、适配、模拟与校验的扩展方式见 [配置处理规则指南](docs/configuration-rule-authoring.md)。

Cisco 和 Juniper 使用纯 Python 手写 Parser 和可变 AST，提供统一 Visitor、Listener、Editor 和 Writer。规则失败时恢复树副本，输出统一排版；详见[手写 AST 指南](docs/config-ast.md)。Cisco 可运行规则模板位于 `gns3_config_adaptor/impl/cisco_iosxrv9k_7_11_1_config_mapping_handler/rules/examples.py`，简单清洗继续使用 YAML。

拓扑预检后会展开常见的 IOS XR `group/apply-group` 和 Junos `groups/apply-groups` 配置，使继承的接口及认证配置进入后续转换。已应用 group 无法静态求值时转换失败，不输出可能不完整的设备配置。

group 展开是事务式的，并遵循显式配置、嵌套层级和 group 列表顺序的厂商优先级。冲突详情写入 `report.json` 的 `group_conflicts` 字段。

## 代码边界

- `gns3_config_adaptor/impl/cisco_iosxrv9k_7_11_1_config_mapping_handler/syntax/`、`gns3_config_adaptor/impl/cisco_iosxrv9k_7_11_1_config_mapping_handler/operations/`、`gns3_config_adaptor/impl/cisco_iosxrv9k_7_11_1_config_mapping_handler/support/`：IOS XR 语法、业务操作和镜像约束；XRv9000 规则仍位于 `gns3_config_adaptor/impl/cisco_iosxrv9k_7_11_1_config_mapping_handler/rules/`。
- `gns3_config_adaptor/impl/juniper_vjunos_router_23_2R1_15_config_mapping_handler/syntax/`、`gns3_config_adaptor/impl/juniper_vjunos_router_23_2R1_15_config_mapping_handler/operations/`、`gns3_config_adaptor/impl/juniper_vjunos_router_23_2R1_15_config_mapping_handler/support/`：Junos 语法、业务操作和镜像约束；vMX 规则仍位于 `gns3_config_adaptor/impl/juniper_vjunos_router_23_2R1_15_config_mapping_handler/rules/`。
- `gns3_config_adaptor/impl/<vendor>/operations/interfaces/`：按 `access`、`analysis`、`business`、`transform` 和 `capabilities` 拆分接口能力。
- `gns3_config_adaptor/impl/common/config_ast/`：两家厂商共享的 AST、遍历、Editor 和 Writer 协议。
- `gns3_config_adaptor/impl/common/domain/`、`gns3_config_adaptor/impl/common/rules/`、`gns3_config_adaptor/impl/common/support/`：公共领域数据、规则引擎、路径清洗及基础设施能力。
- `gns3_config_adaptor/impl/common/domain/capabilities/`：Document 核心协议，以及接口、聚合、Group、清洗、裁剪、兼容、校验、引用和模拟等窄能力协议。
- `gns3_config_adaptor/impl/common/semantic_identity/`：路径感知的语义 identity 规则引擎。
- `gns3_config_adaptor/impl/common/domain/contracts.py`：保留 `VendorConfiguration` 兼容别名；最终类型为组合窄能力的 `VendorDocument`。
- `gns3_config_adaptor/adaptation/topology.py`：Excel 拓扑读取、预检和输出。
- `gns3_config_adaptor/adaptation/groups.py`：Group 展开的跨设备调度和诊断汇总。
- `gns3_config_adaptor/adaptation/interfaces.py`：接口预清理、分类、非 NNI 裁剪、引用改写、目标兼容与最终校验。
- `gns3_config_adaptor/adaptation/nni.py`：NNI 聚合规划、M-LAG 边界、端口分配和配置迁移。
- `gns3_config_adaptor/adaptation/cleaning_rules.py`：声明式与代码型清洗规则、内置注册表及固定执行点调度。
- `gns3_config_adaptor/adaptation/input_normalization.py`：按厂商规则删除原始配置两端的采集回显，再交给语法解析器。
- `gns3_config_adaptor/adaptation/simulation.py`：模拟参数适配调度。
- `gns3_config_adaptor/adaptation/pipeline.py`：显式组合转换阶段，并在首次错误后停止。
- `gns3_config_adaptor/adaptation/service.py`：组织输入加载、完整转换和最终输出。

`CiscoDocument` 和 `JunosDocument` 是配置树聚合根，只负责语法、树状态、修订、
编辑事务、分析缓存和最终渲染。业务动作通过按需创建的轻量能力入口执行，例如
`document.interfaces.analysis`、`document.interfaces.transform`、
`document.validation`、`document.cleaning` 和 `document.simulation`。能力对象
不长期保存在 Document 上，也不缓存配置树状态。所有能力门面统一位于
`operations/`；`rules/` 只保存规则定义、Visitor 和执行机制。

## 测试数据

单元测试按关注点拆分到独立的 `tests/test_*.py` 文件（端到端转换、Cisco/Junos group 展开、AST、清洗、模拟适配与接口业务分析），设备配置一律以 `.cfg` 文件放在 `tests/fixtures/`，拓扑以双 sheet 的 `topology.xlsx` 表达，不嵌入 Python 代码：

- `iosxr_bundle/`：IOS XR 聚合 NNI、非 NNI 清理、group 和认证清洗样例。
- `junos_bundle/`：Junos 聚合 NNI、非 NNI 清理、groups 和认证清洗样例。
- `iosxr_duplicate_vlan/`：带重复 VLAN 的非 NNI 业务裁剪样例。
- `iosxr_mlag/`：IOS XR 同一 Bundle 跨对端拆分、一对多引用与非 NNI 业务裁剪样例。
- `junos_mlag/`：Junos ae 跨对端拆分、非 NNI 接口与网关清理样例。
- `cross_vendor/`：Cisco XRv9000 与 Juniper vMX 互联时，普通 NNI 和 `Bundle-Ether`/`ae` 聚合两端独立适配样例。
- `huawei_peer/`：华为设备只作为拓扑对端，Cisco/Juniper 端仍完成 NNI 适配的样例。
- `junos_unresolved/`：无法解析 group 时事务回滚样例。
- `group_configs/`：独立的厂商 group 优先级、通配、`inactive`/`protect`、引号组名与 `apply-groups-except` 配置。
- `washing_configs/`：管理面必清项、默认保留项及五类扩展清洗样例。
- `simulation_adaptation/`：目标镜像模拟参数适配与幂等性样例。
- `cisco_ast/`：IOS XR 配置层级、`!` 边界与编辑样例；历史解析和编辑回归样例另见 `antlr_cisco/`。
- `junos_ast/`：Junos 行内块、注释、hostname 与括号边界样例。
- `semantic_identity/`：规则 identity 覆盖关系与未知语句降级样例。
- `interfaces/`：接口识别、业务闭包分析与 BVI/VLAN 推断样例。

每个端到端样例目录包含一个双 sheet 的 `topology.xlsx` 和对应的 `configs/*.cfg`。

## 安装与运行

```bash
python3 -m pip install -e .
```

```python
from pathlib import Path

from gns3_config_adaptor.adaptation import convert

context = convert(
    topology_path=Path("topology.xlsx"),
    config_dir=Path("configs"),
    output_dir=Path("output"),
    profiles_path=Path("config/image_profiles.yaml"),
)
if context.has_errors:
    raise RuntimeError(context.errors)
```

`profiles_path` 可配置镜像名称/版本、数据接口列表和模拟参数适配策略；列表最后三个接口全部空置预留，其余接口可分配给 NNI（默认八口中的前五口）。清洗规则位于项目配置目录，按厂商分别位于 `config/rules/cisco_iosxr/xrv9000/cleaning.yaml` 和 `config/rules/juniper_junos/vmx/cleaning.yaml`，转换时不接受外部规则文件。

转换流程已移除原始配置语义审计，不再在映射前扫描原配置的标量冲突或接口角色等语义问题。必要语法解析、拓扑与接口映射冲突检查、最终目标兼容和残留引用校验继续执行。`convert()` 和 `prepare_context()` 保留 keyword-only 参数 `strict_input_validation=False`，该参数只影响仍保留的业务校验，不会重新启用源配置审计。两厂商校验能力不再提供专用的 `validate_source_semantics()` 入口。

`convert()` 会另外生成只含聚合指标的 `performance.json`，覆盖输入准备、各转换阶段与完整输出耗时，并标明进程生命周期内存高水位。公司内正常计时和独立 profile 的运行方式见 [第二轮性能复测说明](docs/performance/second-round.md)。

第三轮已加入增量接口索引、语义规则候选预编译和任务内 LRU。`convert(..., cache_budget_bytes=2 * 1024**3)` 默认允许新增缓存共享约 2 GiB 的估算预算，按需分配；设为 `0` 可关闭这些输入缓存作对照，静态规则预编译仍生效。任务完成、失败或中断后释放缓存，`performance.json` 的 `cache` 字段只保留命中、逐出、估算峰值等聚合指标。这不是进程总内存上限，缓存也不保证每种输入都更快；实测和限制见 [第三轮性能报告](docs/performance/third-round/README.md)。

原始采集文本的输入整理规则分别位于两家厂商镜像目录的 `input_normalization.yaml`。每条规则的 `match` 都是逐行正则列表；单项匹配一行，多项匹配连续多行。`region` 只能是 `leading` 或 `trailing`，匹配后的动作固定为删除。规则 `id` 可只写局部名称，加载时自动补上 `cisco.` 或 `juniper.` 前缀；已带本厂商前缀的 ID 原样使用。规则只从文件两端向内删除完整匹配的行序列；正文中的匹配内容原样保留，清理后只剩空白也直接返回。Juniper 默认规则识别 `show configuration` 回显、`Last commit`、常见尾部提示符、采集分隔符和三行 `{ / master / }` 尾块；Cisco 规则文件默认留空，待确认具体采集格式后添加。输入整理命中情况写入 `report.json` 的 `input-normalization` 事件，不记录被删除的原文。

每个镜像 Profile 的 `simulation_adaptation.mode` 支持 `off`、`compatible` 和 `stable`。默认 `stable` 仅保证已映射数据口启用，并将配置中已经存在且低于安全下限的 BFD interval/multiplier 调高；不会自动开启 BFD，也不会修改 OSPF、IS-IS 或 BGP 的业务定时器。全部修改写入 `report.json` 的 `simulation-adaptation` 事件。仅支持 `simulation_adaptation` 节点。

管理账号、AAA/TACACS/RADIUS、SSH/Telnet 网络管理服务、SSH 信任、SNMP 始终清理；聚合成员绑定与 LACP 模式保留，缩减成员时单独适配门限。简单清洗由 YAML 描述，banner、认证替换等复杂清洗由代码规则实现，两类规则统一由 `CleaningRulesHandler` 在固定执行点调度并按 `category` 报告。协议认证、PKI、硬件、NAT、流量统计等 YAML 规则默认启用，可按 `enable` 单独关闭。

group 统一采用全量处理：静态展开并校验所有活动的 group 引用及其依赖，成功后移除 group 定义和控制语句。未被活动配置引用的模板不参与求值；缺失引用、循环依赖或无法安全静态解释的活动 group 会使转换整体回滚。Junos 的 `inactive:` 配置不参与求值，若父节点保留则随之保留；重复的顶层 `groups {}` 容器及同名定义会先合并再求值。

group 叶子语句使用 XRv9000/vMX 路径感知规则判定语义冲突。未匹配规则且同路径同命令族出现不同值时，固定按 `warn` 处理：保留语句并报告潜在歧义。报告中的 `group-identity-coverage` 事件和 summary 字段给出规则命中数、降级数及实际覆盖率。

Cisco 与 Juniper 的跨厂商 NNI 会按链路两端各自的厂商语法和镜像 Profile 独立分配接口，不要求两端目标接口同名。转换器不审计链路两端的 IP、VLAN、MTU、封装或 Bundle/LACP 协商方式能否实际建立，这些业务一致性由输入配置保证。

华为设备只作为拓扑对端：转换器不改写、不生成华为配置；如配置目录中存在对应文件，则只读提取端口聚合和 M-LAG 配对信息。普通外部链路仍可不提供华为配置；需要压缩或核验多成员聚合时，缺少配置或端口信息会明确报错。与华为相连的 Cisco/Juniper 端点仍按本端镜像 Profile 分配 NNI 目标口并改写本端配置和拓扑接口，华为侧设备行和接口名保持原样。华为与华为之间的链路同样保留在适配拓扑中，但不产生接口映射；未知厂商仍按不支持处理。

华为只读提取支持物理口中的 `eth-trunk N`、Eth-Trunk 下的 `trunkport` 成员列表/同前缀 `to` 范围，以及 `dfs-group N m-lag M`。M-LAG 配对需要 DFS `source ip/ipv6 ... peer ...`（含 `dual-active detection` 形式）的双向地址，或拓扑中明确连接双方 `peer-link N` 成员口的链路；单独的相同 DFS、Eth-Trunk 或 M-LAG 编号不足以配对。配置路径沿用设备表“配置文件”列，未指定时按设备名在配置目录中查找 `.cfg/.conf/.txt`。报告通过 `external-aggregation-read` 和 `external-mlag-verified` 记录读取与配对结果，不包含配置原文。

实验只适配 Excel 中的 NNI 及其聚合、子接口闭包。其他物理、聚合接口及子接口连同协议、VRF、桥域中的引用一并删除，不再汇聚到 UNI 或生成 QinQ。BVI/IRB 仅在剩余 NNI 桥域仍依赖它时保留原有网关类型。Loopback、管理口以及未识别接口仍遵循保守保留策略。

目标接口兼容处理独立于模拟参数策略运行，不能通过 `simulation_adaptation.mode: off` 或 `strict_input_validation` 关闭。已知不适用于物理口或子接口的命令按规则清理或调整，目标镜像无法接受的已知冲突仍使转换失败；报告记录处理原因。静态校验只覆盖已建模规则，自动测试通过不等于已在真实 XRv9000 上验证。当前未连接实机，交付后还须在实际镜像版本上完成 load/commit、接口与路由邻接验收。

被删除接口的 BGP 本地源、显式出接口静态路由和业务绑定会同步裁剪；无法确定语义的依赖保留原配置并记录 warning，不阻断输出。最终校验只豁免 prune 精确标记的“路径 + 接口”项，其他悬空引用仍报错。跳过链路的端点受保护，其占用端口也不会重新分配给其他 NNI。接口名先按厂商规范化，Profile 中使用全名和缩写重复指定同一端口会报错。接口硬件属性作为目标镜像兼容要求无条件清理，不受模拟模式或 `remove_physical_interface_knobs` 控制。

裁剪按 VRF／routing-instance 区分地址与 VLAN 作用域，BGP 邻居的有效显式本地源不会因组默认源失效而被误删。聚合 NNI 保留 Bundle/ae 及子接口，只映射物理成员并压缩同一对端的冗余链路；跨两台华为对端的聚合，只有配置和拓扑能确认同一 M-LAG 时才允许，每台华为分别保留最早一条成员链路，本端同一逻辑聚合保留两个物理成员；其余跨对端聚合仍拒绝，不克隆业务或展开本地源。Profile 中所有数据接口（包括预留口）必须是可识别的物理父口。

实验密码为 `admin123`。IOS XR 使用 `root` 本地用户，Junos 仅设置 root 认证；仅用于隔离的 GNS3 实验环境。

IOS XR 认证清洗同时删除自定义 `usergroup`、`taskgroup` 以及 `line` 下对旧认证、授权、计费和用户组的引用；`exec-timeout` 等非认证终端参数保留。删除结果写入 `cisco.replace.authentication` 清洗事件的 `removed_by_type`。

XRv9000 7.11.1 的默认适配会识别 IOS XR `interface preconfigure`，并在接口分类前通过原子文本重写删除这些尚未实例化的接口候选配置。默认启用的 `cisco.remove.ptp.interface` 规则会在接口迁移和引用改写完成后删除实验不需要的 `interface PTP...` 虚拟接口块。

## 默认 Excel 列

设备列表：`设备名称`、`厂商`、`配置文件`。链接表：`A端设备`、`A端接口`、`Z端设备`、`Z端接口`。程序同时识别常见中英文别名。

## 输出

- `topology-adapted.xlsx`
- `configs/*.cfg`
- `interface-mapping.json`
- `report.json`
- `README.txt`

同目录重跑时，先前发布的配置、拓扑和说明会隔离到 `.previous-outputs/`，其位置记入报告；本次加载或校验失败时不会继续留下可被当成本次成果的旧导出。内部 `.generated-artifacts.json` 记录本程序负责的文件，额外的用户文件保持原位。

聚合识别从拓扑物理口的成员绑定出发，聚合及子接口不需要出现在 Excel 中。逻辑接口 `action=preserve` 自映射；成员 `action=map-member` 映射物理口。带地址、VRF、L2 transport、bridge/unit/family 或子接口的成员保留为单链路并告警，不参与冗余成员压缩；冲突聚合绑定仍报错。没有拓扑物理成员的聚合作为非 NNI 删除；拓扑成员与配置绑定无法对应时才报错。Junos 硬件清洗按叶子匹配，保留 `802.3ad` 和 ae 的必要 options；Cisco 保留成员 `bundle id`。静态/LACP 模式不单边更改。

XRv9000 保留 Bundle 时还需在实际镜像核验 Bundle MAC 与虚拟网卡的聚合支持。Cisco 输入未显式设置 Bundle MAC 时会给出核验告警，不自动编造 MAC 或更改全局 LACP 标识；自动测试不替代聚合 up、commit 和邻居建立验证。
