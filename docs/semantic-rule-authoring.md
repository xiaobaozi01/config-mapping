# 语义 identity 规则编写指南

语义规则用于回答：“同一配置路径下，两条 group/显式配置是否占用同一个配置槽位？”

内置规则分别位于：

- XRv9000：`src/config_adaptor/cisco/rules/xrv9000/`
- vMX：`src/config_adaptor/juniper/rules/vmx/`

identity 规则按 `interfaces.yaml`、`routing.yaml`、`policy.yaml` 和 `system.yaml` 分类。在已有文件中追加规则时不需要修改 manifest；新增 identity 规则文件时，必须同时把文件名加入对应的 `manifest.yaml.rule_files`。

同目录下的 `cleaning.yaml` 是配置清洗规则，由 `CleaningRulesHandler` 单独加载，不属于 identity 规则，也不加入 `manifest.yaml.rule_files`。

需要跨节点扫描、条件判断或创建配置树的复杂清洗规则实现 `ExecutableCleaningRule`，并在 `adaptation/cleaning_rules.py` 的内置注册表中显式注册；YAML 规则固定在 `post_rewrite` 执行。

## 完整示例

Junos 物理接口可以同时配置 up/down hold time：

```text
hold-time up 1000;
hold-time down 640;
```

同一方向的不同数值应当相互覆盖，up 和 down 则应同时保留。对应规则放在 `juniper/rules/vmx/interfaces.yaml`：

```yaml
- id: vmx.interface.hold-time
  node: leaf
  path: [interfaces, "{interface}"]
  statement: [hold-time, "{direction:up|down}", "{milliseconds:uint}"]
  identity: [hold-time, "{direction}"]
  merge: directional
```

展开后的 identity 为：

```text
hold-time down 640  -> directional:hold-time down
hold-time down 0    -> directional:hold-time down   # 同槽位，发生覆盖
hold-time up 1000   -> directional:hold-time up     # 不同槽位，可以并存
```

## 字段说明

| 字段 | 含义 | 要求 |
| --- | --- | --- |
| `id` | 规则的稳定唯一名称，同时写入冲突报告的 `rule_id` | 整个规则包内不能重复；推荐 `<image>.<domain>.<property>` |
| `node` | 规则匹配的 AST 节点类型 | 当前 group identity 合并使用 `leaf`；`block` 为预留类型，暂不用于块合并 |
| `path` | 叶子语句的父级完整路径，每个列表元素对应一个 AST 层级 | 可使用 capture 和 `**` |
| `statement` | 待匹配的叶子语句 token 模板 | 固定关键字直接写，变量使用 capture |
| `identity` | 匹配后生成的配置槽位键 | 只包含决定“是否同一槽位”的部分，不要无条件放入配置值 |
| `merge` | identity 的结构类别，也是最终键的前缀 | 必须是支持的 merge 类型之一 |

`path` 是父节点路径，不包含当前叶子语句。例如：

```text
interfaces {
    ge-0/0/0 {
        unit 0 {
            family inet {
                mtu 1500;
            }
        }
    }
}
```

`mtu 1500` 的 path 是：

```yaml
[interfaces, "ge-0/0/0", "unit 0", "family inet"]
```

注意 `family inet` 是一个 AST 层级，不能拆成 `family` 和 `inet` 两层。

## Capture 语法

| 语法 | 含义 | 示例 |
| --- | --- | --- |
| `{name}` | 匹配一个 token | `{interface}` |
| `{name...}` | 匹配剩余 0 到多个 token | `{options...}` |
| `{name:uint}` | 匹配非负整数 | `{asn:uint}` |
| `{name:ip}` / `{name:ip-address}` | 匹配 IPv4/IPv6 地址 | `{peer:ip}` |
| `{name:prefix}` / `{name:ip-prefix}` | 匹配 IPv4/IPv6 前缀 | `{route:ip-prefix}` |
| `{name:a|b|c}` | 匹配枚举中的一个值 | `{direction:in|out}` |
| `**` | 仅在 path 中使用，匹配 0 到多个 AST 层级 | `[protocols, bgp, "**"]` |

`{name...}` 必须是当前 path 组件或 `statement` 的最后一个 token。capture 名在同一条规则中重复出现时，实际值必须相同。

## Merge 类型

| `merge` | identity 应如何设计 | 典型示例 |
| --- | --- | --- |
| `replace` | identity 排除配置值，同路径只有一个槽位 | `mtu`、`peer-as` |
| `keyed-set` | identity 包含对象 key，排除其他属性 | `address <prefix>`、`network <prefix>` |
| `set` | identity 包含整个集合元素，不同元素并存 | logging/NTP server |
| `directional` | identity 包含 `in/out`、`input/output` 或 `up/down` | filter、route-policy、hold-time |
| `presence` | identity 只表示开关槽位 | `shutdown/no shutdown`、`passive` |
| `ordered-list` | identity 通常排除列表内容，将整个有序列表视为一个属性 | Junos `import/export [ ... ]` |
| `opaque` | 完整文本区分，不主动覆盖 | 规则未命中时的引擎 fallback，通常不手写 |

两条语句只有在 `merge` 和展开后的 `identity` 都相同时才会被视为同一槽位。真正决定覆盖边界的是 `identity`；`merge` 用来明确表达该 identity 的语义类别。

## 测试规则

每增加一条规则，至少验证一组“应相同”和“应不同”的 identity：

```python
def test_junos_directional_rule_example(self):
    path = ["interfaces", "ge-0/0/0"]
    inherited_down = resolve_junos_identity("hold-time down 640;", path=path)
    local_down = resolve_junos_identity("hold-time down 0;", path=path)
    local_up = resolve_junos_identity("hold-time up 1000;", path=path)

    self.assertEqual(inherited_down.identity, local_down.identity)
    self.assertNotEqual(inherited_down.identity, local_up.identity)
    self.assertEqual(inherited_down.rule_id, "vmx.interface.hold-time")
```

然后执行：

```bash
PYTHONPATH=src python3 -m unittest tests.test_semantic_identity -v
PYTHONPATH=src python3 -m unittest discover -s tests
```

对于高风险规则（如 BGP policy、地址或列表），还应增加一个完整 `expand_groups()` 测试，同时验证输出配置、`conflicts` 和 `rule_id`。

## 规则优先级和安全降级

多条规则命中时，引擎按路径字面量、具体路径层级、statement 字面量和 typed capture 选择更具体的规则，不依赖 YAML 顺序。如果两条最具体规则仍完全同级，引擎直接报错，不猜测。

未命中规则的语句使用完整规范化文本作为 `opaque` identity，不会静默删除。同路径同首关键字出现不同未知值时，由 `group_handling.unknown_identity` 决定保留、告警或回滚。
