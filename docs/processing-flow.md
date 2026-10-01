# Cisco IOS XR 与 Juniper Junos 配置处理流程

## 1. 文档目的

本文档说明配置自适应系统如何把真实 Cisco IOS XR 和 Juniper Junos 配置转换为适用于 GNS3 XRv9000 与 vMX 的配置。

内容按照“整体流程 → 公共处理 → 厂商处理 → 具体配置改写”的顺序展开，重点说明：

- Excel 拓扑如何参与接口分类。
- Cisco 和 Juniper 配置如何解析。
- Group 如何展开以及冲突如何处理。
- NNI 为什么必须先于 UNI 处理。
- 聚合接口如何扁平化。
- UNI 如何汇聚到统一物理接口。
- 认证和权限配置如何清洗。
- 接口引用、报告和输出如何生成。

## 2. 输入与输出

### 2.1 输入

系统接收三类输入。

#### Excel 拓扑

Excel 至少包含两个工作表：

- `设备列表`：设备名称、厂商、配置文件。
- `链接表`：A 端设备、A 端接口、Z 端设备、Z 端接口。

链接表中出现的接口均被视为 NNI 物理接口。

#### 设备配置

- Cisco：IOS XR 文本配置。
- Juniper：Junos 大括号层级配置。
- Huawei：当前版本不转换，相关设备和涉及 Huawei 的链路标记为跳过。

#### 镜像接口配置

镜像 Profile 定义 XRv9000 和 vMX 可以使用的数据接口，并按照列表位置划分用途：

```text
interfaces[:-1]  → NNI 可分配接口
interfaces[-1]   → UNI 统一父接口
```

### 2.2 输出

成功转换后生成：

```text
output/
├── topology-adapted.xlsx
├── configs/
│   ├── R1.cfg
│   ├── R2.cfg
│   ├── J1.cfg
│   └── J2.cfg
├── interface-mapping.json
├── report.json
└── README.txt
```

- `topology-adapted.xlsx`：更新 NNI 接口并删除冗余聚合成员行。
- `configs/*.cfg`：适配后的设备配置。
- `interface-mapping.json`：源接口到目标接口的结构化映射。
- `report.json`：错误、告警、Group 冲突、清洗和转换事件。
- `README.txt`：实验账号和输出文件说明。

## 3. 总体处理流程

```text
读取 Excel 拓扑
        ↓
识别设备厂商并加载配置文件
        ↓
加载 XRv9000/vMX 镜像接口 Profile
        ↓
按厂商解析配置
        ↓
预检链路端点并标记不支持的链路
        ↓
按策略展开配置 Group（默认仅业务相关）
        ↓
处理 NNI 和聚合扁平化
        ↓
处理 UNI 和 VLAN 汇聚
        ↓
更新协议和策略中的接口引用
        ↓
执行显式启用的可选能力清洗
        ↓
清洗认证与权限配置
        ↓
按镜像 Profile 适配模拟参数
        ↓
执行可选外部清洗规则
        ↓
生成配置、拓扑、映射和报告
```

核心转换采用显式 Pipeline。每个 Stage 不持有下一阶段，由 Pipeline 统一保证顺序并在首次错误后停止：

```text
TopologyPreflightHandler
        ↓
GroupExpansionHandler
        ↓
InterfaceClassificationHandler
        ↓
NNIHandler
        ↓
UNIHandler
        ↓
ReferenceRewriteHandler
        ↓
OptionalFeatureWashingHandler
        ↓
AuthWashingHandler
        ↓
SimulationAdaptationHandler
```

任一阶段产生错误后，责任链停止，不继续执行后续阶段。

NNI 中的聚合链路识别先由纯函数 `plan_nni_components()` 生成不可变的分组计划，校验成功后 Stage 才分配目标端口并修改拓扑和厂商配置。UNI 的 VLAN 冲突由 `allocate_uni_vlans()` 完成。这两类规划函数不依赖 Cisco/Junos AST，因此可以独立测试；实际配置修改仍通过统一厂商配置接口分派。

`TopologyPreflightHandler` 在任何配置改写前验证链路端点，标记超出支持范围的链路，并为本端生成 `action=skip` 的 NNI 映射。后续 Group、NNI 和 UNI 只消费有效链路或明确保留的接口角色，避免跳过的 NNI 被误分类为 UNI。

`GroupExpansionHandler` 支持三种模式：`relevant` 只展开接口迁移、协议接口引用、管理认证及已启用可选清洗所需的 group；`strict` 展开所有已应用 group；`preserve` 保留全部 group。默认使用 `relevant`，未被选中的定义、`apply-group(s)` 和排除语句继续保留。

`InterfaceClassificationHandler` 使用厂商物理接口白名单分类接口。已知虚拟接口和未知接口均不参与 NNI/UNI 物理端口分配；未知接口原样保留并写入告警，若链接表把非物理接口作为端点则转换失败。

## 4. 输入准备

### 4.1 厂商识别

设备列表中的厂商名称会被规范化：

```text
Cisco / IOS XR / 思科       → cisco_iosxr
Juniper / Junos / vMX / 瞻博 → juniper_junos
Huawei / 华为               → huawei
```

未知厂商属于输入错误。

### 4.2 配置文件定位

设备列表指定配置文件时，系统从配置目录下读取该文件。

未指定时，依次尝试：

```text
设备名.cfg
设备名.conf
设备名.txt
```

所有候选路径都会先规范化，并确认仍在用户指定的配置目录中，防止通过 `../` 读取目录外文件。

### 4.3 NNI 初始定义

Excel 链接表提供 NNI 的权威来源。例如：

```text
R1 Gi0/0/0/0 ←→ R2 Gi0/0/0/1
```

表示两端接口都是 NNI。配置中未出现在链接表的业务接口，才可能在后续被归类为 UNI。

当链路连接 Cisco 与 Juniper 时，两端分别使用本端解析器和镜像 Profile 完成接口分配及引用改写，因此无需目标接口同名。该阶段只执行端口自适应，不比较两端的 IP、VLAN、MTU 或封装；业务一致性由输入配置负责。

## 5. 为什么先处理 NNI，再处理 UNI

接口分类关系是：

```text
全部业务接口
├── Excel 接口及其关联聚合接口 → NNI
└── 排除 NNI 后的剩余业务接口 → UNI
```

因此 UNI 是 NNI 集合确定后的差集，而不是与 NNI 相互独立的分类。

### 5.1 普通接口依赖

如果 UNI 先执行，NNI 映射还没有生成，链接表中的普通物理接口可能被误判为 UNI，并被提前迁移到统一 UNI 子接口。

### 5.2 聚合接口依赖

Excel 记录的是聚合物理成员，但真正承载地址和协议的是逻辑聚合口。

Cisco 示例：

```text
Gi0/0/0/0 ─┐
            ├── Bundle-Ether10
Gi0/0/0/1 ─┘
```

Juniper 示例：

```text
ge-0/0/0 ─┐
           ├── ae0
ge-0/0/1 ─┘
```

如果 UNI 先处理，`Bundle-Ether10` 或 `ae0` 没有直接出现在 Excel 中，可能被误判为 UNI，导致后续 NNI 无法正确扁平化。

### 5.3 目标接口依赖

NNI 阶段还负责确定：

- 哪些源接口属于 NNI。
- 哪些逻辑聚合口属于 NNI。
- 哪些目标镜像物理接口已被占用。
- 哪些成员接口和逻辑接口需要删除或迁移。

因此接口分类相关阶段的正确顺序是：

```text
Group → NNI → UNI
```

完整责任链还会在它们之前执行拓扑预检，并在之后执行引用更新、可选能力清洗、认证替换和模拟参数适配。

如果未来需要交换 NNI/UNI 执行顺序，必须先增加独立的“接口分析与规划阶段”，一次性计算完整 NNI 闭包、UNI 集合和目标端口，不能简单交换两个处理器。

## 6. 公共 Group 处理原则

Group 必须在接口分类之前展开，因为接口、地址、VLAN、协议和认证配置都可能来自 Group。

两个厂商统一遵循以下原则：

1. 显式配置高于继承配置。
2. 更内层应用的 Group 高于更外层应用的 Group。
3. 同一 Group 列表中靠前的 Group 优先。
4. 使用完整配置路径和语义键判断冲突。
5. Group 展开采用事务模式。
6. 任一已应用 Group 无法安全求值时，整台设备回滚。

Group 展开失败后：

- 原配置保持不变。
- Group 定义和应用语句仍然保留。
- 不继续执行 NNI、UNI、配置清洗和参数适配。
- 转换状态标记为失败。

## 7. Cisco IOS XR 处理流程

### 7.1 配置解析

IOS XR 配置首先按照顶层非缩进行和 `!` 分隔符切成配置块。

输入：

```text
interface GigabitEthernet0/0/0/0
 description TO-R2
 ipv4 address 10.0.0.1 255.255.255.252
!
```

内部结构：

```text
CiscoBlock
├── header: interface GigabitEthernet0/0/0/0
└── lines
    ├── description TO-R2
    └── ipv4 address 10.0.0.1 255.255.255.252
```

每个块包含 `active` 状态。删除配置时通常将其标记为无效，最终渲染时跳过，从而避免频繁修改列表顺序。

### 7.2 接口名规范化

拓扑可能使用接口缩写，配置可能使用完整名称。系统统一处理常见前缀：

```text
Gi  → GigabitEthernet
Te  → TenGigE
Hu  → HundredGigE
Fo  → FortyGigE
BE  → Bundle-Ether
Lo  → Loopback
```

例如：

```text
Gi0/0/0/0 → GigabitEthernet0/0/0/0
```

### 7.3 Cisco Group 展开

#### 收集 Group

系统识别：

```text
group GROUP-NAME
 ...
end-group
```

此阶段只收集定义，不修改原配置。

#### 安全检查

以下情况无法静态展开：

- `apply-group` 引用未定义 Group。
- Group 包含 `$` 运行时变量。
- Group 内再次应用其他 Group。
- 配置结构无法可靠求值。

#### 临时语法树

普通配置块会临时按照缩进转换成树，用完整层级判断冲突。例如：

```text
router ospf 1
 area 0
  interface GigabitEthernet0/0/0/0
   cost 10
```

#### 优先级

从高到低：

```text
显式配置
更内层 apply-group
更外层 apply-group
同一 apply-group 列表中靠前的 Group
```

例如：

```text
apply-group FIRST SECOND
```

当 `FIRST` 和 `SECOND` 配置相同语义键时，`FIRST` 优先。

#### 正则选择器

支持引号包裹的接口正则：

```text
interface 'Gig.*'
interface 'GigabitEthernet0/0/0/.*'
```

多个选择器命中同一接口时，字面内容更长、匹配更具体的选择器优先；长度相同则按表达式词法顺序处理。

#### 语义冲突

系统不会只比较第一个单词。

以下两条不是冲突：

```text
ipv4 address 10.0.0.1 255.255.255.0
ipv4 access-group ACL-IN ingress
```

典型单值配置：

- `description`
- `mtu`
- 主 IPv4 地址
- `shutdown/no shutdown`
- `router-id`
- 指定方向的 ACL 或 service-policy

典型可重复配置：

- IPv6 地址
- BGP network
- 不同 logging host
- 不同方向的策略

#### 提交

只有全部 Group 成功展开后才会：

- 删除 Group 定义。
- 删除 `apply-group` 和 `exclude-group`。
- 写回展开后的配置。
- 记录冲突的胜出来源和值。

### 7.4 Cisco NNI 处理

#### 普通 NNI

普通 NNI 按 Excel 行号稳定排序并分配镜像物理接口。

系统执行：

1. 规范化源接口名。
2. 分配下一个可用 NNI 目标接口。
3. 改名接口块及子接口。
4. 保留地址、描述、MTU、VRF 等配置。
5. 记录结构化接口映射。

#### 聚合成员识别

系统通过以下配置识别 Bundle 成员：

```text
interface GigabitEthernet0/0/0/0
 bundle id 10 mode active
```

物理成员对应逻辑接口：

```text
Bundle-Ether10
```

#### 聚合扁平化

当多条 Excel 链路在同一对端设备之间属于同一 Bundle 时：

1. 使用并查集将成员行归为一条逻辑链路。
2. 保留 Excel 中行号最小的成员链路。
3. 删除其他冗余成员链路。
4. 删除原物理成员接口配置。
5. 将 Bundle 及其子接口配置迁移到目标物理接口。
6. 删除 `bundle id`、LACP 和聚合成员属性。
7. 记录成员删除、父接口迁移和子接口迁移映射。

同一对端分组内，多个成员行的两端必须各自指向唯一聚合。
单边聚合或一端同时指向多个聚合时，无法在不猜测配置冲突的前提下合并，
因此按保守策略报错。

#### M-LAG 按对端拆分

同一 Bundle 的物理成员连到不同对端时，不会跨对端合并：

1. 分组键为“本端设备 + Bundle + 对端设备”。
2. 每个对端组分配一个目标物理口。
3. Bundle 及子接口先克隆到各目标的占位接口。
4. 删除旧 Bundle 和所有成员后，再把占位接口落到真实目标名。
5. 全局 Bundle 引用展开为多条目标引用。

接口改名使用两阶段占位符：

```text
源接口 → ADAPT-NNI-* → 最终目标接口
```

这样可以避免接口互换或名称重叠引起连续替换。

### 7.5 Cisco UNI 处理

UNI 候选必须满足：

- 不属于 NNI。
- 不是 Loopback。
- 不是管理接口。
- 不是最后一个 UNI 目标父接口。
- 不是聚合物理成员。
- 自身配置 IP/L2VC/L2 transport，或被 L2VPN、bridge-domain、路由协议等全局业务引用。

所有 UNI 映射到最后一个镜像接口的子接口。
只有描述、MTU 等非业务属性的裸口会被删除，不消耗模拟器资源。

BVI 不会因为自身存在 IP 地址就自动迁移。系统先从活跃 L2 attachment
circuit 收集 VLAN，并分析 `bridge-domain` 中的 `routed interface`；只有
与活跃业务关联的 BVI 才迁移，未关联的网关作为无效 UNI 删除。

#### VLAN 分配

原 VLAN 满足以下条件时保留：

```text
2 ≤ VLAN ≤ 4094
设备内未发生重复
```

否则从 2 开始选择最小空闲 VLAN。

例如两个 UNI 都配置 VLAN 100：

```text
第一个 UNI → 保留 VLAN 100
第二个 UNI → 重新分配 VLAN 2
```

#### UNI 配置迁移

例如：

```text
GigabitEthernet0/0/0/3.100
→
GigabitEthernet0/0/0/7.100
```

系统会：

1. 暂时把源父接口改为 `ADAPT-UNI-*`。
2. 将每个源接口迁移到目标 UNI 父接口。
3. 删除原 encapsulation、rewrite、Bundle 和 LACP 属性。
4. 写入新的 `encapsulation dot1q <outer> second-dot1q <inner>`。
5. 合并可能出现的重复目标接口块。
6. 删除源接口。

如果目标父接口不存在，自动创建：

```text
interface GigabitEthernet0/0/0/7
 no shutdown
```

### 7.6 Cisco 认证与权限清洗

以下顶层配置整块删除：

```text
username
aaa
tacacs-server / tacacs
radius-server / radius
taskgroup / task-group
usergroup / user-group
snmp-server
ssh server / ssh client
telnet server
```

`line` 配置只删除认证相关子命令：

```text
password
secret
login authentication
authorization
accounting
users group
```

以下非认证终端参数继续保留：

```text
exec-timeout
session-timeout
transport
```

最后添加实验账号：

```text
username labadmin
 secret 0 Gns3Lab@2026
 group root-system
```

`root-system` 是 IOS XR 内置权限组，不需要重新创建 taskgroup 或 usergroup。

Bundle/聚合 UNI 扁平化到普通物理口时，`bundle minimum-active links/bandwidth`、LACP 和其他聚合专属属性由 NNI/UNI 阶段清理，不进入认证处理器。

## 8. Juniper Junos 处理流程

### 8.1 配置解析

Junos 使用大括号表达层级：

```text
interfaces {
    ge-0/0/0 {
        unit 0 {
            family inet {
                address 10.0.0.1/30;
            }
        }
    }
}
```

系统使用栈解析成树：

```text
interfaces
└── ge-0/0/0
    └── unit 0
        └── family inet
            └── address 10.0.0.1/30
```

节点包含：

- `header`：当前语句。
- `children`：大括号中的子节点。
- `active`：是否输出。
- `origin`：显式配置或来源 Group。
- `rank`：Group 继承优先级。

配置大括号不平衡时直接报错。

`inactive:` 和 `protect:` 前缀在语义比较时被去除，但原始节点仍然保留。

### 8.2 Junos Groups 展开

#### 事务副本

系统首先深拷贝整棵配置树，所有 Group 操作都在副本上完成。

#### Group 定义

系统识别：

```text
groups {
    GROUP-A { ... }
    GROUP-B { ... }
}
```

#### Group 应用

支持：

```text
apply-groups GROUP-A;
apply-groups [ GROUP-A GROUP-B ];
apply-groups-except GROUP-A;
```

Group 定义内可以继续应用其他 Group。系统递归求取依赖闭包，在真实配置
层级上动态展开，因此间接 Group 中的通配节点和局部
`apply-groups-except` 仍按最终接口路径生效。未应用 Group 所引用的定义会
继续保留，避免相关 Group 展开后产生悬空引用。

优先级从高到低：

```text
显式配置
更内层 apply-groups
更外层 apply-groups
同一列表中靠前的 Group
```

`apply-groups-except` 会阻止指定 Group 在当前层级及其子层级继续继承。

#### 通配节点

支持常见 Junos 通配选择器：

```text
<ge-*>
<ge-0/0/*>
<*>
```

多个选择器同时命中时，更具体的选择器优先。

#### 语义冲突

典型单值语句：

- `description`
- `mtu`
- `vlan-id`
- `encapsulation`
- `host-name`
- `router-id`
- `class`
- `metric`
- `preference`

`address` 等可重复语句按照完整内容区分，不会因为关键字相同被错误覆盖。

#### 回滚条件

以下情况回滚：

- `apply-groups` 引用未定义 Group。
- Group 直接或间接形成循环引用。
- 配置层级无法安全求值。

成功后才停用原 `groups` 块，并删除已经处理的 apply 语句。

### 8.3 Junos NNI 处理

#### 普通 NNI

普通接口直接改名到目标 vMX 物理接口，原 unit 跟随父接口迁移。

例如：

```text
xe-0/0/3.0 → ge-0/0/0.0
```

#### 聚合成员识别

系统通过 `802.3ad` 识别 ae 成员：

```text
ge-0/0/0 {
    gigether-options {
        802.3ad ae0;
    }
}
```

#### ae 扁平化

聚合 NNI 处理步骤：

1. 找出所有引用同一 `ae` 的物理成员。
2. 将 Excel 成员行合并为一条逻辑链路。
3. 删除原物理成员接口。
4. 将 `ae0` 改名为目标物理接口。
5. 保留 `ae0` 下的 unit、地址和协议配置。
6. 删除 `aggregated-ether-options`、`gigether-options` 和 `ether-options`。
7. 更新协议中的 `ae0` 或 `ae0.0` 引用。

若同一 ae 的成员连到不同对端，与 IOS XR 相同，按对端拆分为多个物理口，ae 配置和全局引用都会一对多展开。

### 8.4 Junos UNI 处理

所有 UNI 汇聚到最后一个 vMX 接口。

系统从活跃 unit 的 `vlan-id`、`vlan-id-list`、`vlan members` 收集业务
VLAN，并沿 `bridge-domains`/`vlans` 的接口、VLAN ID或名称查找 IRB。
只有关联活跃广播域的 IRB 才会迁移。

目标父接口自动具备：

```text
flexible-vlan-tagging;
encapsulation flexible-ethernet-services;
```

迁移过程：

1. 找到源接口和源 unit。
2. 深拷贝 unit 配置。
3. 根据 VLAN 规划修改 unit 编号。
4. 递归删除原 `vlan-id`、`vlan-id-list`、`vlan-tags`、`vlan members`、接口模式和输入/输出 VLAN map。
5. 写入新的 `vlan-tags outer <outer> inner <inner>`。
6. 将 unit 添加到统一目标父接口。
7. 全部 unit 迁移完成后停用源父接口。

例如：

```text
ge-0/0/3.100 → ge-0/0/6.100
```

如果源接口没有 unit，系统会把接口下配置包装成临时 `unit 0`，再迁移到新 VLAN unit。

### 8.5 Junos 认证与权限清洗

系统在 `system` 下删除：

```text
login
root-authentication
authentication-order
radius-server
tacplus-server
radius-options
tacplus-options
accounting
```

同时删除顶层 `snmp`、`system services` 下的 SSH/Telnet、嵌套的 NETCONF-over-SSH，以及 `security ssh-known-hosts`。GNS3 的 Telnet console 连接由虚拟机串口提供，不依赖设备自身的 Telnet server。

删除整个 `system login` 会同时清除：

- 原本地用户。
- 自定义 login class。
- `user remote` 模板。
- 用户密码和 SSH key。
- allow/deny commands 权限限制。

随后重新生成：

```text
system {
    root-authentication encrypted-password "...";
    login {
        user labadmin {
            class super-user;
            authentication {
                encrypted-password "...";
            }
        }
    }
}
```

- `super-user` 是 Junos 内置 class。
- 密码使用 SHA-512 crypt 哈希。
- root authentication 用于保证配置可以正常提交。

### 8.6 可选扩展清洗

`--washing-policy` 读取独立 YAML，以下开关默认均为 `false`：

```yaml
optional_washing:
  protocol_authentication: false
  pki: false
  hardware: false
  nat: false
  flow_statistics: false
```

- `protocol_authentication`：删除 OSPF/IS-IS/BGP 等协议认证定义和引用。
- `pki`：删除明确的 PKI、证书及密钥生成配置。
- `hardware`：删除目标虚拟镜像不需要的硬件、槽位或 chassis 配置。
- `nat`：删除 NAT/CGN 配置。
- `flow_statistics`：删除流量采样和流量监控配置。

这些开关会改变业务能力，只有显式启用才由 `OptionalFeatureWashingHandler` 执行；它与 `AuthWashingHandler` 的强制管理面替换分开报告。清洗报告只记录类别和数量，不记录秘密值。

### 8.7 模拟参数适配

镜像 Profile 中的 `simulation_adaptation` 控制 `SimulationAdaptationHandler`：

```yaml
simulation_adaptation:
  mode: stable
  ensure_data_interfaces_enabled: true
  remove_physical_interface_knobs: true
  bfd_minimum_interval_ms: 300
  bfd_minimum_multiplier: 3
```

- `off`：保持所有参数不变；YAML 中建议写成 `"off"`，避免被解析为布尔值。
- `compatible`：保证已映射的数据父接口启用，并删除这些目标口上的 speed、duplex、FEC、协商、链路防抖等物理设备属性。
- `stable`：包含 compatible 行为，并只对配置中已经存在的 BFD interval/multiplier 执行下限钳制。

适配不会为原本未启用 BFD 的协议或接口新增 BFD，也不会默认修改 OSPF、IS-IS、BGP 的 hello/hold/SPF 等业务定时器。非映射接口不参与接口属性清理。处理结果按设备写入 `report.json` 的 `simulation-adaptation` 事件；重复执行不会继续产生变更。旧 `param_adjustment` YAML 节点仍兼容读取，但不能与新节点同时配置。

## 9. 全局接口引用更新

NNI 和 UNI 处理过程中会生成全局接口映射，例如：

```text
Bundle-Ether10     → GigabitEthernet0/0/0/0
Bundle-Ether10.100 → GigabitEthernet0/0/0/0.100

ae0                → ge-0/0/0
ae0.0              → ge-0/0/0.0
```

映射用于更新：

- OSPF
- IS-IS
- BGP
- MPLS
- L2VPN
- VRF
- ACL
- QoS
- Segment Routing
- Telemetry

常规映射是一对一。M-LAG 按对端拆分时，同一源 Bundle/ae
会生成多个目标；Cisco 将相关命令行/配置块复制展开，Junos
将 interfaces 之外的相关节点克隆展开。

Cisco 会更新非接口配置块头和块内容中的引用。

Junos 会跳过 `interfaces` 定义本身，只递归更新其他层级中的引用，避免对已经迁移完成的接口节点再次替换。

## 10. 可选清洗规则

核心转换后可以加载外部 YAML 规则：

```text
delete  → 删除匹配配置
replace → 替换匹配内容
mask    → 隐藏敏感内容
warn    → 保留配置但记录告警
```

示例：

```yaml
rules:
  - id: remove-hardware-feature
    vendor: cisco_iosxr
    match: "^hw-module"
    action: delete
    reason: "XRv9000 不支持真实硬件模块配置"
```

每次命中都会在 `report.json` 中记录规则 ID、动作、命中数量和原因。

## 11. 失败处理

以下情况必须将转换标记为失败：

- Excel 缺少必需工作表或列。
- 设备名重复。
- 链路端点信息不完整。
- 配置文件不存在或越出配置目录。
- 厂商无法识别。
- NNI 数量超过镜像可用物理接口数量。
- 同一对端聚合组内的成员关系不一致。
- UNI VLAN 空间耗尽。
- Group 无法完整展开。
- Junos 大括号不平衡。
- 配置结构无法安全解析。

失败时仍写出：

```text
interface-mapping.json
report.json
```

但不生成设备配置和适配后的拓扑，防止用户误用不完整结果。

## 12. 报告内容

`report.json` 主要包含：

- 总体成功或失败状态。
- 全局和设备级错误。
- 告警。
- 拓扑预检和跳过链路事件。
- Group 展开事件。
- Group 冲突路径、语义键、胜出值和被覆盖值。
- 聚合链路扁平化事件。
- M-LAG 按对端拆分的源接口、目标接口和 Excel 行。
- 接口映射数量。
- 有效和跳过链路数量。
- 可选能力清洗分类统计。
- 认证清洗分类统计。
- 模拟参数适配分类统计。
- 外部规则命中记录。

认证报告只记录删除类别和数量，不记录原密码、密钥或其他认证秘密。

## 13. 当前边界

当前版本不处理：

- Huawei 配置转换。
- 涉及 Huawei 的跨厂商 NNI。
- IOS、IOS XE 或 NX-OS 到 IOS XR 的语法转换。
- UNI 的真实 GNS3 外部连接。
- 包含运行时变量且无法静态求值的复杂 Group。
- 未通过内置逻辑或外部规则明确指定的大范围硬件配置删除。

对于无法安全处理的配置，系统优先失败或保留并告警，不进行猜测式改写。
