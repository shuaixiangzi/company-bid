# 日志、Trace 与断点调试

这是首版基础功能。每个节点必须能解释“用了什么输入、执行到哪里、调用了谁、为什么失败、如何继续”，并支持反复单环节试验。全自动制作与调试共用同一执行器和节点代码。

## 四类记录各司其职

| 记录 | 内容 | 存放与用途 |
|---|---|---|
| 执行记录与 checkpoint | job/node attempt 状态、游标、token、输入输出引用、重用关系 | PostgreSQL，恢复与调度的权威 |
| 结构化日志 | 时间、级别、事件、稳定错误码、标识、耗时、数量、受限异常 | 本地滚动 JSON 日志，用于检索故障 |
| Trace | API、入队、Worker、节点、Provider/OCR、文件和渲染调用的关联与耗时 | OpenTelemetry，持久记录与查看端 |
| 受控运行产物 | 冻结输入、节点 JSON、校验报告、实际模型请求/响应、OCR结果、最终文档 | 受权限控制的文件存储，以 hash 和身份引用 |

业务审计单独记录谁启动、取消、重跑、修改资料或读取敏感诊断。审计与业务动作同事务；不把完整日志塞进审计表。

## 统一关联字段

每条关键事件携带 `request_id、project_id、run_id、job_id、job_attempt_id、node_key、node_attempt_id、trace_id、span_id` 中适用的字段。外部调用再加 `call_id、call_attempt、provider_request_id`；分窗/分页/章节加 `unit_id`。没有的字段显式缺省，不伪造标识。

记录模型/Provider 标识、提示词模板版本、节点实现版本、schema 版本、输入输出 hash、等待与执行耗时、状态、错误码、重试理由、token/费用信息。Provider 没有返回用量时记 unknown，不记 0。区分一个逻辑调用与实际发生的多次 HTTP 请求。

应有稳定事件：`job.queued/claimed/lease_lost/cancel_requested`，`node.started/checkpointed/failed/reused/paused`，`call.started/completed/failed/retry_scheduled`，`artifact.stored/published`，`diagnostics.capture_failed/export_failed`。异常堆栈受权限保护并脱敏，面向普通用户的错误摘要只给可执行原因。

## Trace 实现

采用 OpenTelemetry Python SDK，为节点和外部调用显式建 span；API/HTTP 可用标准 instrumentation 补充。跨 API 入队和 Worker 的上下文通过任务元数据传递。每次恢复或重跑有新的执行 trace，以 Span Link 关联前次尝试和复用产物，不维持一条跨多天的活动 span。OpenTelemetry 提供 attributes、events、exception 与 span links。[官方 Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)

在 2～3 人场景下，业务节点和外部调用按 100% 记录设计，控制单条属性长度与事件数量，不把正文塞进 span。基础环境提供本地持久 span 记录及可重试导出；调试部署提供 Jaeger 单节点查看端，采用持久卷而非内存模式。Badger 是 Jaeger 的嵌入式持久后端，适合单节点、有限数据量，具体版本和保留配置在实现时验证并锁定。[Jaeger Badger 文档](https://www.jaegertracing.io/docs/2.23/storage/badger/)

Jaeger 使用 all-in-one 配置，使采集和查询访问同一嵌入式存储；不把两个独立容器分别指向同一个 Badger 目录。

不为首版搭建 Kafka、Elasticsearch 或独立可观测集群。Jaeger 停机不影响 checkpoint 和恢复，诊断页显示 trace 导出缺口；导出恢复后能补传本地已保存记录。持久 span 格式使用明确的标准映射，不另造第二套业务事件模型。

Worker 强制退出时，尚未完成的内存 span 可能不完整。数据库中的 started 记录和租约回收会标记 interrupted；显示 trace_partial，不声称发生过完整的结束事件。实现时必须测试落盘、缓冲、进程退出、导出失败与补传，不能仅以引入 SDK 宣称链路完整。

## 输入输出与敏感信息

节点输入/输出、冻结版本、校验报告与 hash 默认持久保存，才能可靠单步恢复。使用受控文件引用，不在日志中重复正文。

为反复调试提供 `debug_capture`：调试运行默认开启，保存实际发送的 prompt/消息、参数、脱敏后的请求头、完整响应、解析结果和校验失败内容；普通运行也能按超管策略启用。捕获策略在 run 开始时冻结，界面明确显示 `captured、disabled_by_policy、write_failed、partial`。关闭捕获不影响基本 trace，但不能声称可重放完整调用。

成员仅查看本人项目的状态、进度、业务产物与安全错误摘要；超管可查看全部项目的诊断、节点输入输出和调用原文。所有诊断查询、导出及两个 run 的对比都检查角色和对象范围；全局读取权限不扩大为操作他人标书的权限。

密钥、Authorization、Cookie、签名 URL 凭据在序列化前剔除；招标正文和公司资料仍是敏感内容，只有授权用户通过应用读取。自动 HTTP/异常 instrumentation 同样不能把完整 URL 查询参数、请求体或异常回显泄露到日志/span。

建议初始保留：普通日志/trace 30 天、完整调用内容 14 天，可配置；错误诊断可按项目保留。招标原件、已发布文件和被有效 run 引用的必要 checkpoint 不跟随短期日志清理。清理先核对引用，再在受限目录执行；不能因日志到期破坏断点续跑。

诊断导出默认仅含元数据、版本与脱敏错误；包含正文、prompt 或原件的导出需在界面明确内容范围并走访问授权。诊断产物不提交 Git。日志/trace 本地写入失败要可见；checkpoint 本身不能写入则节点失败，禁止继续推进。完整 debug 捕获失败时标明缺口并停在可诊断边界，不能默默宣称完整采集。

## 三种执行操作

| 操作 | 行为 | 版本与费用语义 |
|---|---|---|
| 继续失败或暂停任务 | 同一 run，从最近有效 checkpoint 开始，只补未完成单元 | 冻结输入及执行版本须一致；已成功单元不重新计一次模型调用 |
| 只运行下一节点 | 同一执行器运行到指定节点成功 checkpoint；还有后续节点时 paused_debug，最终节点发布完成则 succeeded | 释放租约/执行槽；关闭网页或重启后仍可继续 |
| 从指定节点重新运行 | 新建子 run，记录 parent_run_id 与 fork_from_node；重算选定节点及其所有后续 | 合法前缀产物引用复用；旧产物和最终文件保留，可对比新旧 |

“返回上一步”是选择上一个节点作为新分支起点，不回滚删除旧事实。已经成功的节点如需重新试验，也走新分支。节点内的页/窗口/章节可作为精细重试单元，但必须有完整输入和依赖引用。

可细分节点使用稳定 unit_id；每个成功页/窗口/章节的输入 hash、输出引用、版本和 attempt 单独持久登记，并接受相同的 token/取消校验。全部必要单元有效后才提交节点汇总 checkpoint。三类单元共用一种通用记录结构，不各自建一套业务表。

上传文件、公司资料版本、模板、模型、prompt 或节点代码改变时创建新 run，不悄悄继续旧 run。界面提示最早受影响的节点；例如只修改 N4 的 prompt 可以复用 N0～N3，修改招标原件则从 N0 开始，修改公司材料通常从首次消费该材料的节点起失效。

可提供“使用已记录响应”的离线调试模式验证解析/渲染逻辑，必须标为 replay；它不代表再次调用了模型，不计作新的真实效果验证，默认不发布为普通制作结果。实际重跑可能再次产生 Provider 费用，页面展示预计调用范围和历史消耗，不承诺外部调用 exactly-once。

## 依赖与 checkpoint 规则

缓存/复用键覆盖节点真正消费的上游产物 hash、相关公司资料版本、节点 schema/实现版本、prompt、模型标识及影响结果的参数。版本清单由系统生成，不能依赖用户手填缓存键。

复用前检查产物存在、hash、schema、来源权限和生产版本；公司公共资料可跨成员任务使用，私有项目的 checkpoint 和资料不得被另一成员任务复用。新 run 记录 reused_from_artifact_id，不能把复用显示成重新执行。节点重新执行时其下游原有产物全部失效；未涉及的合法前缀保持只读引用。

执行器提交 checkpoint 时锁 job、核验 token/取消和期望 revision，原子写产物引用、节点完成、下一节点游标及执行记录。写文件成功但 DB 提交失败不是有效 checkpoint。提交结果不明先对账。未完成单元可在原 run 继续；既有成功产物丢失或 hash 错误则标 checkpoint_invalid、禁止复用，并创建修复子 run 从最早受影响节点重算，原记录和错误保留，不能在原 run 覆盖已成功产物。

同一 job 只有一个有效 lease。同一用户从多个页面同时点继续，只能入队一次；其他成员不能继续该用户的任务。旧 attempt 迟到不能覆盖新 attempt。单步暂停和失败不占用活跃槽，调试分支受与正常任务相同的全局资源配额。

## 调试页与接口草案

调试页左侧是 N0～N6 时间线，显示每次尝试和复用；右侧可查看输入/输出、来源、schema 校验、错误与 trace。支持两个尝试的结构化差异：配置变化、输入 hash、要求数、章节数、缺项变化、实际调用、耗时和用量。

命令接口草案：`resume(expected_revision, mode)`、`step(expected_revision, stop_after_node)`、`rerun_from(node, overrides, parent_run_id)`、`cancel(expected_revision)`；全部鉴权、幂等并可审计。普通列表读取接口不能触发写操作。

先提供 CLI/API 单步能力并测试，再做页面按钮。无需可拖拽流程图、任意代码执行或模型控制节点走向。

## 必须通过的调试验证

1. N2 三个窗口中一个失败，续跑只调用该窗口，其他结果 hash 不变。
2. 单步到 N3 后关闭浏览器并重启服务，仍能从该暂停点继续。
3. 从 N3 返回 N2 重跑，新 run 只复用 N0/N1，N2 及后续重新计算，旧 DOCX 不变。
4. 修改 N4 prompt，系统禁止混用旧 N4/下游缓存，并能展示两次差异。
5. Provider 已返回但 checkpoint 未提交时崩溃，显示该 attempt 未完成；允许重试并如实记录可能的重复调用，只发布一次。
6. checkpoint 已提交但客户端未收到确认，恢复按幂等键对账，不重复执行成功节点。
7. 三用户对各自项目同时继续/取消/创建重跑分支，不串输入、无重复领取，等待资源可见；交换 project/run ID 后读取及操作均被拒绝，超管可全量查看但不能据此修改他人任务。
8. token 已更新，旧 attempt 的节点和最终发布均被拒绝。
9. trace 后端停机、日志磁盘故障和进程强杀分别有可见采集状态，不能假报完整。
10. 密钥、源文 canary 不进入普通日志/span/错误响应；受控产物只在授权范围可读。
11. 成功 checkpoint 文件损坏后创建修复子 run，不覆盖原尝试；单步到 N6 发布后直接 succeeded，不残留 paused_debug。
