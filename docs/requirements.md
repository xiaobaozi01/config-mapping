# GNS3 路由器配置自适应系统需求规格

## 1. 文档目的

本系统将真实网络中的 Cisco IOS XR 与 Juniper Junos 配置，以及描述设备和物理链路的 Excel 拓扑，转换为可导入 GNS3 的 XRv9000 与 vMX 配置。系统重点解决虚拟镜像接口数量、接口名称和真实设备不一致的问题，并清理实验环境不可用的认证配置。

本文档是首版实现和验收的基线。华为配置转换及涉及华为设备的跨厂商 NNI 不在首版范围内。

## 2. 输入

### 2.1 拓扑文件

输入为 `.xlsx` 文件，包含两个工作表：

- `设备列表`：设备名称、厂商、配置文件。
- `链接表`：A 端设备、A 端物理接口、Z 端设备、Z 端物理接口。

程序兼容需求中定义的常用中英文列别名。额外工作表和额外列应原样保留。链接表中出现的物理接口均为 NNI。

### 2.2 配置文件

- Cisco 输入配置：IOS XR 文本配置。
- Juniper 输入配置：Junos 大括号层级格式。
- 设备列表未指定配置文件时，程序按设备名称匹配 `.cfg`、`.conf` 或 `.txt`。

### 2.3 镜像接口配置

镜像配置文件描述 XRv9000 和 vMX 在 GNS3 中的镜像名称、可选版本、实际可用的数据接口列表和模拟参数适配策略。列表最后一个接口承载 UNI 子接口，其余接口按链接表顺序分配给 NNI。

## 3. 术语和分类

- NNI：端口出现在 Excel 链接表中。
- UNI：配置中存在，但未被链接表引用的物理或聚合业务接口。
- Loopback：逻辑环回接口，编号和地址保持不变。
- 管理接口：XR 的 `MgmtEth` 及 Junos 的 `fxp`、`em` 等接口，不参与业务接口映射。
- 聚合接口：IOS XR 的 `Bundle-Ether` 或 Junos 的 `ae`。

## 4. 功能需求

### 4.1 NNI映射

1. NNI 按 Excel 链接表行号稳定排序。
2. 每条有效 NNI 链路在两端分别按本端厂商和镜像 Profile 分配目标物理接口；Cisco XRv9000 与 Juniper vMX 可以互联，两端目标接口名称不要求一致。
3. 若多个物理成员属于同一聚合链路，系统在同一对端设备范围内执行扁平化：
   - 保留最早的一条物理成员链路；
   - 删除其余冗余成员链路；
   - 将聚合接口及聚合子接口配置迁移到保留链路对应的目标物理接口；
   - 删除成员关系、LACP 和空聚合接口；
   - 删除 minimum-active links/bandwidth 等只适用于聚合接口的门限；
   - 更新所有对聚合接口的配置引用。
4. 同一 `Bundle-Ether`/`ae` 的成员若连到不同对端，识别为 M-LAG 形态：
   - 按对端设备分组，每组单独占用一个模拟器物理口；
   - 聚合及其子接口/unit 配置复制到各组目标口；
   - 协议和 L2VPN 中的旧聚合引用按目标数量展开。
5. 普通 NNI 物理接口的配置和全局引用改为目标物理接口。
6. NNI 数量超过可分配物理接口时，转换失败并报告错误。
7. 系统不审计链路两端的 IP、VLAN、MTU 或封装一致性，此类业务一致性由输入配置保证。

### 4.2 UNI映射

1. NNI 处理完成后，仅剩余有效业务 UNI 映射到目标镜像最后一个数据接口的子接口。
2. UNI 不生成 GNS3 拓扑链路。
3. 有效业务的识别条件为：配置 IP，或承载 L2VC/L2Circuit、L2 transport/CCC/bridge，或被 L2VPN、bridge-domain、VLAN、routing-instance 及路由协议引用。
4. 物理口下的子接口/unit、聚合关系和业务引用作为一个依赖闭包处理。Cisco 沿 `l2transport/bridge-domain` 关联 BVI，Juniper 沿 `bridge-domains`/`vlans` 的接口、VLAN ID或名称关联 IRB；只有广播域仍包含活跃 UNI 时，网关才作为 UNI 逻辑口迁移。BVI 接口编号不得默认解释为 VLAN ID；只有同一 bridge-domain 存在唯一明确的接入口标签时，才能将其作为网关的内层业务 VLAN。
5. 无 IP、无 L2 绑定且无外部业务引用的裸口不保留；无业务聚合的成员口一并删除。
6. 目标 UNI 统一使用 QinQ，原 VLAN 标签匹配、rewrite、`vlan-id-list`、`vlan members`、接口模式及 VLAN map 配置清除后重写；`vlan-ccc` 等决定业务类型而非标签匹配的封装语义保留：
   - 外层 VLAN 在设备内唯一，同时作为目标子接口/unit 编号；
   - 可识别原 VLAN 时，优先将其作为内层 VLAN；
   - 原 VLAN 无法识别时，内外层都使用自动分配值。
7. 外层 VLAN 分配策略：
   - 原 VLAN ID 合法且未冲突时保留；
   - 无 VLAN 或发生冲突时，从 `2..4094` 选择最小空闲 VLAN；
   - 目标子接口或 Junos unit 编号等于目标 VLAN ID。
8. UNI 的地址、VRF、描述、协议和策略引用应尽量保留，并通过全局接口映射同步更新。
9. UNI 数量超过 VLAN 可用空间时，转换失败。

### 4.3 Loopback和管理口

- Loopback/`lo0` 原样保留。
- 数据接口列表最后一个接口专用于 UNI，不再额外保留数据接口。
- 管理接口不参与转换，设备通过 GNS3 console 管理。

### 4.4 认证清洗

系统删除原配置中的本地账号、TACACS+、RADIUS、外部 AAA、SSH/Telnet 网络管理服务、SSH 信任和 SNMP 信息，并生成固定实验账号：

```text
用户名：labadmin
密码：Gns3Lab@2026
```

- IOS XR 删除自定义 `usergroup`、`taskgroup`、`snmp-server`、SSH/Telnet server/client，以及 `line` 配置中对旧密码、认证方法、授权方法、计费方法和用户组的引用；保留与认证无关的 console 参数。新用户直接加入内置 `root-system`。
- Junos 删除原 `system login`、RADIUS/TACACS server/options、accounting、`system services` 下的 SSH/Telnet、`security ssh-known-hosts` 和顶层 `snmp`。新用户使用内置 `super-user`，输出配置使用密码哈希；同时配置 root authentication 以保证配置可提交。
- `report.json` 按账号、AAA、TACACS、RADIUS、usergroup、taskgroup 和 line 引用等类别记录删除数量。
- 转换报告不得回显被删除的秘密值。

协议认证、PKI、硬件、NAT 和流量统计属于默认关闭的扩展清洗。用户通过独立 YAML 开关显式启用；未提供策略文件时必须保留这些配置。协议认证启用后，密钥定义和协议引用必须同时清理。

### 4.5 保守清洗和扩展规则

除认证外，系统默认保留未知配置。可确认不兼容的配置通过外部 YAML 规则扩展。规则动作至少支持：

- `delete`：删除匹配配置。
- `replace`：替换匹配值。
- `mask`：清除秘密值但保留结构。
- `warn`：不修改，仅报告。

首版允许规则框架存在而只内置少量安全规则；任何规则命中均记录规则 ID 和原因。

### 4.6 全局接口映射

系统维护结构化映射，而非简单字符串字典。每条记录至少包含设备、源接口、角色、动作、目标接口、关联 Excel 行和原因。映射模型支持一对多：M-LAG 拆分时，同一源 Bundle/ae 可对应多个目标物理口。接口块、路由协议、MPLS、VRF、L2VPN、ACL、QoS等对接口的引用均使用该映射更新。

### 4.7 责任链

转换核心使用责任链模式。配置 group 必须在接口分类前展开，处理顺序为：

```text
TopologyPreflightHandler -> GroupExpansionHandler -> InterfaceClassificationHandler
-> NNIHandler -> UNIHandler -> ReferenceRewriteHandler -> OptionalFeatureWashingHandler
-> AuthWashingHandler -> SimulationAdaptationHandler
```

每个处理器只负责其领域的识别、映射、配置变更和报告，并将上下文传递给下一个处理器。后续处理器可插入链中而不修改已有处理器调用方式。

- `TopologyPreflightHandler` 在配置改写前校验端点，标记无效或暂不支持的链路；跳过链路的本端接口仍以 `action=skip` 保留 NNI 角色，不得被 UNI 重新分类。
- `InterfaceClassificationHandler` 只允许厂商白名单中的物理接口参与物理端口映射；未识别接口保留原配置并告警，若被链接表用作端点则转换失败。
- `OptionalFeatureWashingHandler` 只处理显式开启的协议认证、PKI、硬件、NAT 和流量统计清洗。
- `AuthWashingHandler` 只负责强制管理面认证清理和实验账号写入；两步视为同一原子阶段。

### 4.8 模拟参数适配

1. `SimulationAdaptationHandler` 在接口迁移、引用更新和认证清洗后运行，根据设备的镜像 Profile 调用厂商文档实现。
2. `simulation_adaptation.mode` 支持：
   - `off`：不执行参数适配；
   - `compatible`：只启用已映射的数据口并清理目标口上的物理硬件属性；
   - `stable`（默认）：在 `compatible` 基础上，将已经存在且过于激进的 BFD interval 和 multiplier 提高到 Profile 指定的安全下限。
3. 参数适配不得凭空开启 BFD，不得默认缩短 OSPF、IS-IS、BGP 等业务协议定时器，也不得修改未参与映射的数据口。
4. 参数适配必须幂等；每台设备的镜像、模式、目标接口和分类修改数量写入 `report.json`。

### 4.9 配置 Group 展开

1. Group 统一展开并校验所有活动引用及其完整依赖，不提供相关性筛选或原样保留模式；未被活动配置引用的模板不参与求值。
2. IOS XR 支持识别并展开活动的 `group ... end-group`、`apply-group`/`apply-groups`：
   - 精确接口选择器展开到对应接口；
   - 引号包裹的接口正则选择器按照已知显式接口和拓扑接口展开；
   - 显式配置优先于 group 中的同类配置；
   - 未定义 group、运行时变量或 group 内再次应用其他 group 等无法安全求值的情况触发转换失败；正则选择器当前无匹配对象时视为无继承结果。
   - 显式配置高于 group；内层 `apply-group` 高于外层；同一条应用语句中靠前的 group 优先。
   - 同一 group 中多个正则匹配时，按最长匹配优先，长度相同按表达式词法顺序处理。
3. Junos 支持识别并展开活动的 `groups {}`、`apply-groups` 和 `apply-groups-except`：
   - 支持根层级和局部层级应用；
   - 多个顶层 `groups {}` 容器必须规范化为一个容器，同名 group 定义的子配置按原始出现顺序合并；
   - 支持 Group 内递归应用其他 Group，并校验完整传递依赖；
   - 间接未定义引用或 Group 循环引用触发事务回滚；
   - 支持常见 `<ge-*>`、`<*>` 通配节点；
   - 显式配置优先于 group 继承配置；
   - 无法安全解析的已应用 group 触发转换失败，不输出不完整的自适应配置。
   - 显式配置高于 group；嵌套层级的 group 高于外层；同一 `apply-groups` 列表中靠前的 group 优先。
   - 所有 `inactive:` 节点及其完整子树必须从自适应配置中删除，不得激活或保留其后代配置。
4. 展开后的配置进入后续 NNI、UNI、Auth 处理器。成功展开后从输出中移除 group 定义及其应用语句，避免接口改名后再次引用旧接口。
5. group 展开必须采用事务模式：所有活动引用及其依赖均可安全解析时才提交展开结果；任一活动 group 无法解析时整体回滚并将本次转换标记为失败。
6. 配置冲突使用完整层级和语义键判断，不能只比较命令首关键字：
   - `ipv4 address` 与 `ipv4 access-group` 是不同配置；
   - Junos多个 `address`、接口列表等可重复配置应同时保留；
   - `mtu`、`description`、`vlan-id` 等单值配置按继承优先级选出唯一值。
7. 每个冲突记录层级、配置键、胜出/被覆盖的来源和值，并输出到 `report.json.group_conflicts`。
8. 配置键由 XRv9000/vMX 专属的路径感知规则包生成；规则未命中时不得静默覆盖任何语句。
9. `group_handling.unknown_identity` 支持 `preserve`、`warn` 和 `fail`；`fail` 遇到同路径同命令族的未知潜在冲突时必须整体回滚。
10. 报告必须输出 group identity 规则命中数、降级数、覆盖率和未知歧义数，供镜像语料持续补齐规则。

优先级语义依据厂商文档：Cisco IOS XR 配置组采用本地配置优先、内层组优先及最长正则匹配；Junos采用本地配置优先、嵌套组优先，且同一列表中第一个 group 优先。

## 5. 输出

输出目录包含：

- `topology-adapted.xlsx`：更新后的有效 NNI 拓扑；冗余聚合成员行被删除。
- `configs/<设备名>.cfg`：转换后的设备配置。
- `interface-mapping.json`：完整接口映射。
- `report.json`：错误、警告、删除和处理摘要。
- `README.txt`：导入说明和固定实验账号。

涉及华为的设备及其链路不转换，并在报告中标记为跳过。

## 6. 失败和告警策略

以下情况必须作为错误，不得输出“成功”状态：

- 必需工作表或列缺失。
- 设备、配置文件或厂商无法识别。
- NNI 物理接口不足。
- 同一对端聚合组内的成员关系不一致。
- UNI VLAN 空间不足。
- 同一有效链路端点映射不完整。
- 配置语法结构无法安全解析。

未知但结构完整的非认证命令默认保留并产生可选告警。

## 7. 非功能要求

- Python 3.11 及以上。
- 相同输入产生稳定的接口和 VLAN 分配结果。
- 不在日志和报告中泄露原认证秘密。
- 转换过程不修改原始文件。
- 核心处理器、Excel解析、厂商配置解析和映射规则应可单元测试。

## 8. 首版不包含

- 华为配置转换。
- 涉及华为设备的跨厂商 NNI。
- IOS/IOS XE/NX-OS 到 IOS XR 的语法转换。
- UNI 的真实 GNS3 外部连接。
- 未经规则明确指定的大范围硬件配置删除。
- 包含运行时变量且无法静态求值的复杂配置 group；此类输入将明确报错，不生成转换配置。
