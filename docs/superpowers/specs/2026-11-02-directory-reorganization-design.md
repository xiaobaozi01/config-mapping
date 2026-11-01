# 厂商实现目录重组设计

## 目标

在不改变配置转换行为的前提下，按技术职责重新组织 `common`、`cisco` 和
`juniper` 目录，使开发者能够从目录名直接判断文件的归属。

本次重组以提交 `035c5d0` 为基线。公共类名、转换流水线阶段和输出格式均保持
不变。

## 非目标

- 不拆分 `groups.py`、`interfaces.py`、`pruning.py` 等大型业务模块。
- 不改变 Cisco 或 Juniper 的配置语义、接口映射、规则优先级或错误策略。
- 不新增 CLI、转换阶段或外部依赖。
- 不为所有旧深层导入路径保留兼容模块，避免重新制造平铺文件。

## 目标结构

```text
src/impl/common/
├── config_ast/
├── semantic_identity/
├── domain/
│   ├── contracts.py
│   ├── interface.py
│   ├── outcomes.py
│   └── policies.py
├── rules/
│   ├── __init__.py
│   ├── engine.py
│   ├── cleaning.py
│   └── empty_blocks.py
└── support/
    ├── config_paths.py
    ├── errors.py
    ├── interface_cache.py
    └── lab_account.py
```

```text
src/impl/cisco/
├── document.py
├── syntax/
│   ├── ast.py
│   ├── parser.py
│   ├── writer.py
│   ├── semantics.py
│   └── rewrite_helpers.py
├── operations/
│   ├── groups.py
│   ├── interfaces.py
│   ├── bundles.py
│   ├── cleaning.py
│   ├── configuration_cleaning.py
│   └── references.py
├── support/
│   ├── clone_constraints.py
│   ├── empty_blocks.py
│   └── identity.py
└── rules/
```

```text
src/impl/juniper/
├── document.py
├── syntax/
│   ├── ast.py
│   ├── parser.py
│   └── writer.py
├── operations/
│   ├── groups.py
│   ├── interfaces.py
│   ├── bundles.py
│   ├── cleaning.py
│   ├── configuration_cleaning.py
│   └── references.py
├── support/
│   ├── clone_constraints.py
│   ├── empty_blocks.py
│   └── identity.py
└── rules/
```

## 职责边界

- `syntax/` 只处理配置语法树、解析、输出和语法级辅助操作。
- `operations/` 提供 Group、接口、聚合、清洗和引用改写等业务能力。
- `support/` 提供镜像输出策略、语义 identity 和业务约束辅助能力。
- `rules/` 保留按执行阶段注册的规则实现和入口。
- `document.py` 继续作为唯一稳定厂商门面，负责组合各职责模块。
- `common/domain/` 保存跨厂商领域协议和数据类型。
- `common/rules/` 保存规则引擎及共享路径清洗、空块策略。
- `common/support/` 保存路径、错误、缓存和实验账号等基础设施。

依赖方向保持为：

```text
adaptation
    -> vendor document / public package exports
        -> vendor syntax / operations / support / rules
            -> common domain / rules / support / config_ast
```

`common` 不得导入 `adaptation` 或任一厂商模块；Cisco 与 Juniper 也不得互相
导入。移动文件后，架构测试应显式验证这套方向。

## 移动映射

| 原路径 | 新路径 |
| --- | --- |
| `common/contracts.py` | `common/domain/contracts.py` |
| `common/interface.py` | `common/domain/interface.py` |
| `common/outcomes.py` | `common/domain/outcomes.py` |
| `common/policies.py` | `common/domain/policies.py` |
| `common/rules.py` | `common/rules/engine.py` |
| `common/cleaning.py` | `common/rules/cleaning.py` |
| `common/empty_blocks.py` | `common/rules/empty_blocks.py` |
| `common/config_paths.py` | `common/support/config_paths.py` |
| `common/errors.py` | `common/support/errors.py` |
| `common/interface_cache.py` | `common/support/interface_cache.py` |
| `common/lab_account.py` | `common/support/lab_account.py` |
| `cisco/ast.py` | `cisco/syntax/ast.py` |
| `cisco/parser.py` | `cisco/syntax/parser.py` |
| `cisco/writer.py` | `cisco/syntax/writer.py` |
| `cisco/semantics.py` | `cisco/syntax/semantics.py` |
| `cisco/rewrite_helpers.py` | `cisco/syntax/rewrite_helpers.py` |
| `cisco/groups.py` | `cisco/operations/groups.py` |
| `cisco/interfaces.py` | `cisco/operations/interfaces.py` |
| `cisco/bundles.py` | `cisco/operations/bundles.py` |
| `cisco/cleaning.py` | `cisco/operations/cleaning.py` |
| `cisco/configuration_cleaning.py` | `cisco/operations/configuration_cleaning.py` |
| `cisco/references.py` | `cisco/operations/references.py` |
| `cisco/clone_constraints.py` | `cisco/support/clone_constraints.py` |
| `cisco/empty_blocks.py` | `cisco/support/empty_blocks.py` |
| `cisco/identity.py` | `cisco/support/identity.py` |
| `juniper/ast.py` | `juniper/syntax/ast.py` |
| `juniper/parser.py` | `juniper/syntax/parser.py` |
| `juniper/writer.py` | `juniper/syntax/writer.py` |
| `juniper/groups.py` | `juniper/operations/groups.py` |
| `juniper/interfaces.py` | `juniper/operations/interfaces.py` |
| `juniper/bundles.py` | `juniper/operations/bundles.py` |
| `juniper/cleaning.py` | `juniper/operations/cleaning.py` |
| `juniper/configuration_cleaning.py` | `juniper/operations/configuration_cleaning.py` |
| `juniper/references.py` | `juniper/operations/references.py` |
| `juniper/clone_constraints.py` | `juniper/support/clone_constraints.py` |
| `juniper/empty_blocks.py` | `juniper/support/empty_blocks.py` |
| `juniper/identity.py` | `juniper/support/identity.py` |

## 公共入口

以下公共入口保持可用：

- `impl.cisco.CiscoDocument`
- `impl.cisco.CiscoNode`
- `impl.cisco.CiscoGroupExpander`
- `impl.juniper.JunosDocument`
- `impl.juniper.JuniperNode`
- `impl.juniper.JunosGroupExpander`
- `impl.common` 当前导出的领域类型、策略和协议
- `impl.common.config_ast` 和 `impl.common.semantic_identity` 的现有入口

深层模块路径属于内部实现，本次允许随目录移动而变化。包级 `__init__.py` 应继续
导出稳定公共对象，内部导入统一改为新路径。

## 实施策略

1. 先建立新目录及 `__init__.py`，按移动映射迁移文件。
2. 机械更新相对导入和 `impl.common.*` 绝对导入。
3. 更新包级公共导出、测试导入和文档中的代码路径。
4. 扩充架构测试，验证公共层、厂商语法层和业务层的依赖方向。
5. 运行完整测试、格式、Lint、函数长度和 docstring 门禁。

## 验收标准

- `common` 根目录只保留 `__init__.py` 和职责子目录。
- `cisco`、`juniper` 根目录只保留 `__init__.py`、`document.py` 和职责子目录。
- 不存在旧平铺文件或兼容转发模块。
- Cisco 与 Juniper 的公共包级导出保持兼容。
- 全部 452 个现有测试及其子测试通过。
- Pyink、Ruff、函数长度和 docstring 检查全部通过。
- 相同输入产生与基线一致的配置、拓扑、映射和报告。
