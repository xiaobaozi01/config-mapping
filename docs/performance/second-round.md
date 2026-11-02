# 第二轮优化：移除源审计，验证完整任务是否达到 10 分钟

## 已实施范围

原版约 70 分钟来自启用 profiler 的测量，不能直接换算成新版正常耗时。本轮先完成可确定的功能裁剪和测量支持，不承诺未经真实数据验证的加速比例。

- 从默认流水线移除源配置语义审计及专用接口范围计算。
- 清理两厂商的源审计注册表和 `validate_source_semantics()` 专用能力入口。
- 保留必要语法解析、拓扑／映射冲突检查、转换前置约束、最终目标兼容性与残留引用检查。共用规则仍用于这些现有职责。
- `convert()`、`prepare_context()` 的调用签名保持兼容，`strict_input_validation=True` 也不会恢复已移除的源审计；参数在剩余业务校验中的作用保留。
- 业务配置处理和逐条审计明细不作裁剪，`report.json` 和接口映射报告继续完整生成。

单任务额外约 2 GiB 的缓存预算是后续优化边界，本轮没有预分配这部分内存，也没有加入未经过新版热点验证的大缓存。这不是进程总 RSS 的限制。

## 新增聚合性能文件

正常调用 `convert()` 后，输出目录增加 `performance.json`。文件记录：

| 字段 | 含义 |
|---|---|
| `status` | 转换成功或失败；业务错误、异常、中断均不会被计为成功 |
| `total_seconds` | 从输入加载前到完整输出处理结束的墙钟时间，不含性能文件自身写入 |
| `stages` | 各阶段名称、耗时和状态，按实际执行顺序记录 |
| `source_semantic_audit` | 固定为 `removed`，明确此次运行没有源审计阶段 |
| `profiler_enabled` / `tracing_enabled` | 检测到的解释器插桩状态 |
| `memory` | 进程生命周期 RSS 高水位，单位为字节；不可获取时为 `null` |

计时覆盖输入准备、清洗规则装配、两轮清洗、拓扑预检、继承展开、接口分类、非 NNI 裁剪、NNI 迁移、引用改写、接口兼容、模拟参数适配、最终校验和输出。异常后的诊断写入另记为 `failure_output`；短路后未运行的阶段不会伪造耗时记录。

性能文件只包含聚合阶段名、数值和状态，不包含配置文本、设备名、接口名、输入路径、错误原文或报告明细。正常返回的 `context.performance.snapshot()` 提供相同聚合快照；任务实例之间独立，不保留历史上下文。

`report.json` 必须在输出阶段内部写入，因此完整输出耗时另存于性能文件，避免把尚未完成的 JSON／拓扑／配置发布时间漏掉。性能文件采用已有原子写入机制，叶子符号链接不会被跟随覆盖外部文件。性能写入故障不改变业务结果或掩盖原始转换异常，记录固定告警并尽力移除旧性能文件，避免误用旧数据。

计时范围是本仓库的 `convert()`。若公司外层调用还包含上传、排队、其他服务调用或下载，应另外记录外层任务起止时间，作为完整任务的最终验收依据。

## 公司内复测

先关闭 PyCharm profiler、调试器、覆盖率及其他插桩工具。标记能识别 Python hook 和新监控槽，但不能证明没有外部进程采样器。尤其新版 cProfile 可以使用 `sys.monitoring`，单独看 `sys.getprofile()` 不可靠。

Windows PowerShell 示例，替换为内部路径，输出目录必须与输入目录分开：

```powershell
$env:PYTHONPATH = "src"
python benchmarks/conversion_retest.py --topology "输入目录\topology.xlsx" --configs "输入目录\configs" --output "性能复测\timing" --warmup 1 --repeat 3
```

每次运行都使用独立输出子目录，避免归档上次产物干扰对比。真实配置及完整报告留在公司环境。脚本另写 `performance-summary.json`，标准输出也仅打印同一聚合结果。

- `measured_median_seconds`：成功且未检测到插桩的正式运行中位数。
- `eligible_for_acceptance`：至少一次成功预热、至少三次正式运行，且所有运行成功、正式运行未检测到插桩。
- `meets_target`：仅满足上述前提时判断中位数是否不超过 600 秒，否则为 `null`。本地小样例满足该条件只代表该样例，不代表真实项目达标。
- 任一运行失败时脚本退出码非零；失败原因及详细报告仍只留在本地。

若实际使用自定义 profiles 或严格策略，请附加原有参数，不改变实际业务条件：

```powershell
python benchmarks/conversion_retest.py --topology "输入目录\topology.xlsx" --configs "输入目录\configs" --profiles "输入目录\image_profiles.yaml" --output "性能复测\timing" --warmup 1 --repeat 3 --strict-input-validation
```

首次只确认能否运行时可以设 `--warmup 0 --repeat 1`；该烟测不判定达标。

单独采集一次新版 profile，不与正常计时混在一起：

```powershell
python benchmarks/conversion_retest.py --topology "输入目录\topology.xlsx" --configs "输入目录\configs" --output "性能复测\profile" --profile --warmup 0 --repeat 1
```

原始 `profile.pstat` 放在对应 `run-*` 子目录，可能包含内部源码路径，应留在公司环境。由内部工具提取匿名化的函数标签、调用次数、自身耗时及累计耗时；累计耗时存在调用关系重叠，不相加估算收益。

只需反馈聚合性能文件、代码版本、匿名化热点表和内部结果一致性结论，不需要提交配置或完整业务报告。

## 内存口径

`peak_rss_at_start` 和 `peak_rss_at_end` 均为整个进程从启动至采样时的驻留内存高水位。长驻服务和顺序复测会继承前面任务的峰值；二者相减不是该任务的真实峰值。

需要比较单次进程峰值时，在新进程中单独运行一次，将其作为独立内存实验，不拿这一次的时间代替预热后的耗时验收。RSS 包含原有 AST、解释器、依赖和输出缓冲，与未来额外缓存预算分开核算。

Unix 使用 Python 的 [resource API](https://docs.python.org/3/library/resource.html)，Windows 使用 [GetProcessMemoryInfo 的 PeakWorkingSetSize](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters)。Windows 原生结构和指针参数已作模拟测试；本机实际运行验证为 macOS，Windows 仍需公司环境确认。

## 第二阶段的执行门槛

只有新版正常测量仍超过目标且新版 profile 支持时，再逐项实现并分别提交：

1. NNI 阶段仍有重复接口查找：建立名称到节点列表、父口到子接口的索引，保留重复定义和源码顺序语义。
2. 语义匹配缓存仍反复失效：将满容量整体清空改为任务内预算 LRU，保留完整上下文键，避免跨任务存留输入结果。
3. 引用／路径计算仍为主要热点：复用同一树状态的索引，修改后失效或增量维护，不缓存旧节点和旧路径。

这些缓存共享额外约 2 GiB 的任务预算，记录命中率、估算占用和实际进程峰值，结束及异常退出均释放。已经达到目标则停止新增改造；没有收益的缓存不保留。

当前没有优化后的真实项目数据，因此尚未触发第二阶段，也不能宣布真实任务已达到 10 分钟。

## 本轮验证记录

- `4ab3a6a`：移除源审计及专用入口。
- `118ab00`：阶段计时、聚合性能文件及复测脚本。
- 全量测试：578 个测试、3,988 个子测试通过。
- src 格式、ruff、两段式 docstring、方法长度检查通过；新增脚本和专项测试另经 ruff 检查。
- 覆盖输入失败、业务失败、阶段异常、输出异常、中断、性能文件写入失败、旧性能文件处理、符号链接、任务隔离、旧／新解释器 profiler 检测及 Unix／Windows 内存后端。
- 公开的小规模 `iosxr_bundle`、`junos_bundle` 样例均完成一次预热和三次正常转换，本机中位数约 43.2 ms／44.2 ms。该结果只验证复测链路正常，不代表真实项目达到 10 分钟。
- 独立 cProfile 样例中 `profiler_enabled=true`，脚本没有将其计为达标；没有真实公司数据的优化后测量，第二阶段尚未触发。
