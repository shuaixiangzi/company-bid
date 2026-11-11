# 现有项目审视

审视日期：2026-11-10。结论是保留可靠执行、来源与文档处理方面的经验，重新建立一条面向“自动生成后线下修改”的小型业务链。旧项目的目录确认、在线编辑兼容、多代策略与发布补偿不适合作为新项目的起始骨架。

## 事实范围

- 主要代码参考：`E:/ai-bid-rebuild-wt-mfs001`，HEAD `53f20a3afac42e07069d58ee53398457ea8b5aed`；代码树对应已审核 `dc5a67873973f7c0a7f81cd0e982918dd4e5182e`，两者之间只有接受记录等文档。
- 样本现状参考：`E:/ai-bid-rebuild-wt-sample001`，已读取提交 `1d30f311413aa12705259bdaead3f0e029dbdf42` 的 current 和 run-a 摘要。该卡仍 in_progress，不能当作已接受的新实现。
- 主检出 `E:/ai-bid-rebuild` 有其他任务未提交修改，仅检查状态，没有把其脏代码作为本计划依据。
- 本轮是静态代码与已记录运行结果审视，没有新跑旧项目测试、模型、OCR 或数据库。旧测试结果不冒充本轮复跑。

## 关键发现与新项目处理

| 已核实的旧实现 | 证据 | 新项目决定 |
|---|---|---|
| 上传已有流式 hash、格式嗅探、文件名边界和不可变文件机制 | `documents/service.py:323`；`files/validation.py:76` | 保留合同，独立实现窄文件接口 |
| 主 workflow 直接调用 parse handler，内层 commit 后又 refresh，外层继续 commit/reload | `workflow/handler.py:1017`；`extraction/parse_handler.py:153` | 解析是应用服务；只有执行器管理节点提交 |
| OCR 覆盖和来源引文有实质校验 | `extraction/ocr/service.py:161`；`source_span.py:90` | 保留逐页/逐块账本和精确引用，不让 UI 简化损害内容覆盖 |
| DOCX 主循环只显式处理正文段落/表格/SDT，存在异常后 continue 和一律 requires_ocr=False | `extraction/parser/docx.py:83`、`:190` | 检测嵌图、复杂对象、失败块；明确支持范围，不假报完整读取 |
| 分章节窗口、覆盖账本和有界重试已经存在 | `requirements/windows.py:89`；`requirements/extraction.py:798` | 保留算法与负例；合并为一个明确节点及通用尝试记录 |
| structured 匹配包含检索、硬条件过滤、主体和有效期判断 | `workflow/handler.py:2893`、`:3109`、`:3280` | 保留判断，不复制 H5/Harness 多代分支；相似度不等于资格满足 |
| 自动目录骨架已能从原文派生，但 workflow 仍停在人工确认/规划 handoff | `composition/outline_plan_service.py:252`、`:302`；`workflow/handler.py:4288`、`:4468` | 自动目录成为内部节点；不伪造人工 confirmed 标志 |
| 旧 draft 入口明确禁止进入旧实现 | `workflow/handler.py:5754`、`:5764` | 不因后面有大量代码就认定已具备可用正文生成 |
| 新 publisher 默认正文主要是公司名、证据 ID 说明和步骤文字，不负责生成技术方案正文 | `simplification/draft_input.py:25`、`:39`、`:45`、`:62`；`simplification/service.py:152` | 实质章节写作是新项目首要能力，不能只改发布状态 |
| 新文档投影将每个 unit 变成一级标题与单段落，requirements/materials/evidence_ids 为空 | `simplification/document.py:73` | 必须真实装配多级章节、表格和原件附件，不能用可打开 DOCX 代替内容验收 |
| 旧 renderer 有表格、图片和页面能力，同时带多代分支；部分目录模式使用估算页码 | `exports/renderer.py:734`、`:1917`、`:2106`、`:2316` | 借鉴小型渲染函数和视觉样例，固定一种文档结构，实际核验目录/分页 |
| 工作稿先提交业务指针，Worker 再单独完成 job；取消保护因此跨多个层次 | `simplification/service.py:414`；`jobs/execution.py:457`；`simplification/pointer_fence.py:151`、`:181` | 新系统统一最终完成事务，取消和发布共用锁；不复制补偿层 |
| 快照模型含 online_word_session_key、parent、edit_mode，另有 current pointer | `bid_assistant/models.py:149`、`:185` | 无在线编辑时每 run 固定结果，省去 current pointer 和文档修订树 |
| 准备页承担公司解析、绑定/冻结、解析等待和启动串联 | `apps/web/src/views/ProjectPrepView.vue:395`、`:551`、`:856` | 一个后端启动命令，前端只展示状态和操作 |

表中 Python 路径相对 `E:/ai-bid-rebuild-wt-mfs001/apps/api/src/ai_bid_api/`。完整源码保留在旧目录，不复制到新项目。

可直接抽查：[工作稿输入](E:/ai-bid-rebuild-wt-mfs001/apps/api/src/ai_bid_api/simplification/draft_input.py:25)、[文档投影](E:/ai-bid-rebuild-wt-mfs001/apps/api/src/ai_bid_api/simplification/document.py:73)、[旧 draft 守卫](E:/ai-bid-rebuild-wt-mfs001/apps/api/src/ai_bid_api/workflow/handler.py:5754)、[Worker 完成路径](E:/ai-bid-rebuild-wt-mfs001/apps/api/src/ai_bid_api/jobs/execution.py:457)。

## 最新样本提供的教训

[样本运行摘要](E:/ai-bid-rebuild-wt-sample001/docs/plan/rebuild/evidence/BID-MAINFLOW-SAMPLE-SLICE-001/run-a.md)记录：首次运行发生 OCR 前置阻断及匹配局部变量错误；后续验证已完成 OCR、提取、匹配与目录确认，提取仍记录 partial_failure。最终 17 条要求里 16 条因 EVIDENCE_PACKAGE_MISSING 阻断，发布返回 409，没有 DOCX。

该次运行没有挂企业资料库。因此这份证据说明“缺资料在旧政策下会整份阻断”，不能据此证明“挂好资料库后匹配仍必然失败”，也不能据此量化新系统质量。

新用户决定已经明确改变产品语义：缺公司资料或报价时，交付带待补标记的文档；技术方案允许基于招标要求起草。新项目应区分资料缺失、方案待编写、输入读取失败，而不是把它们统一变成发布失败。这项改变仅用于新系统，不修改旧项目守卫。

## 什么复用，什么重新做

保留：hash/来源定位、原件与派生资料区分、主体/有效期筛选、分窗和单位级重试、不可变文件、下载授权、租约/fencing、故障/并发反例、表格和页面核验方法。

重新做：简单自动流水线、统一 BidPlan 与章节文档结构、正文生成、公司资料维护界面、最终完成事务、单节点执行器、checkpoint 与调试分支、标准 trace 采集。

放弃首版继承：OnlyOffice、项目聊天改稿、目录在线编辑/人工确认链、当前工作稿指针、在线 revision 树、旧 engine 兼容、多套 Agent runtime、重复业务状态机和大量阶段 CRUD。

复用的对象首先是算法、约束、合成测试和反例。确有收益的小函数也要做来源登记与独立验证，不能整包导入旧 ORM、旧库或工作区路径。旧输出不能直接成为新系统正确答案。

## 既有验证的边界

[R4 审核](E:/ai-bid-rebuild-wt-mfs001/docs/plan/rebuild/evidence/BID-MAINFLOW-SIMPLIFY-001/audit-verdict-r4.md)记录 38+5 个 pytest、9 个 PostgreSQL 调度与目标迁移验证；接受范围是该卡代码和已记录缺口。旧宿主机全量记录为 3142 passed、12 failed、579 skipped、1 error，不是全套 PASS。

旧卡未证明真实标书整体质量、完整部署、负载和备份恢复。新项目的正确性、Word 效果、调试可恢复性和三人并行均按自己的验收计划重新验证。
