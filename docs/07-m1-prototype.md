# M1 文档原型

更新日期：2026-11-11。M1 原型已实现并完成本轮自检，真实结果登记见本页末尾；业务验收仍由使用者核对 Word。没有开始 M2 数据库/服务实现。

用户指定人工标书作为质量基准，并要求先复核旧项目已有设计。[设计复用记录](08-legacy-design-reuse.md)已完成；现已在同一 M1 执行器接入精简写作单元、来源绑定的参考改编、表格完整性校验和冻结样式配置。本次质量升级使用本地审校内容，没有新增 DeepSeek 请求；具体结果与原型边界见下方记录。

产品范围已同步为固定单公司、不核对公司主体；成员各自操作本人标书，超管可查看全部。项目归属与账号权限计划在 M2 实现；当前 M1 仍为受信任本机 CLI，没有据此宣称已经实现用户隔离。

## 已实现范围

- `src/company_bid` 独立 Python 包，严格 PreparedBid/DocumentPackage 类型、来源 hash、事实与拟实施方案分开。
- 人工准确预整理输入 → N0 冻结 → N4 按章节/写作单元生成 → N5 校验 → N6 Word/清单。N1～N3 自动解析/提取/匹配尚未实现，不伪装为自动完成。
- 本地 CLI 同一执行器支持完整运行、单步、同 run 续跑、新子 run 重跑、损坏 checkpoint 修复分支。页/窗口/章节可用同一个通用 unit 合同；本轮实际接入章节和章内写作单元。
- 每个节点、单元和实际 HTTP 尝试留记录；真实 OTel SDK spans 持久落盘。完整输入输出和请求响应保存在受控产物，普通日志/trace 仅含白名单元数据。
- 成功写作单元 checkpoint 不因后续单元、章节或渲染失败重新调用模型。程序、prompt、模型配置改变时创建子 run；配置消费关系决定可复用前缀。仅样式配置/渲染器改变时可从 N6 建子 run，复用有效正文。
- DeepSeek 只接收当前技术章或写作单元、对应招标要求及选中的适用参考片段，输出限制为方案段落、步骤和覆盖定位；不传整本人工标书或无关人员证明。事实/表格/附件由应用装配。
- WritingTask 冻结稳定 ID、目的、must_cover、完成判据、禁止项、格式状态和引用。参考摘录绑定原件、定位、文本 hash、保留/排除范围；未读明或冲突的格式在请求前阻止自由写作。覆盖定位只证明落点，N5 仍标记需要来源实质核对。
- 原件、节点输出及结果不可变；结果读取只返回已发布字节。捕获失败、部分捕获和进程强杀留下的 trace 缺口可见。
- 使用价格上界估算，在请求前持久预留费用；中断或用量未知时不自动返还预留。配置示例限制每次执行 1 个 HTTP attempt、每 run 最多 2 次实际调用和 0.02 美元预留上限。价格仅为冻结配置估算，最终费用以服务商账单为准。[核对的官方接口与价格](https://api-docs.deepseek.com/quick_start/pricing/)

## 本地运行

Python 3.12+。使用 `uv sync --extra test --frozen` 安装锁定依赖；`uv run pytest -q` 验证。运行目录为项目根，CLI 可用 `uv run company-bid`，或设置 `PYTHONPATH=src` 后 `python -m company_bid.cli`。

Windows 首次使用真实资料前执行 `scripts/protect_local.ps1`，将 `.local` 限制为当前账号、SYSTEM 和管理员。真实原件、资料清单、全部运行内容、密钥均留在 `.local`，已从 Git 排除。首版原型是受信任本机管理员 CLI，不提供团队用户鉴权接口。

```powershell
# 人工预整理输入先经过 PreparedBid 校验；离线方案按 unit_id（旧整章输入按 section_id）存储。
company-bid --root .local/runtime create --input .local/sample/prepared.json --proposals .local/sample/proposals.json --stop-after N4
company-bid --root .local/runtime resume RUN_ID --stop-after N6
company-bid --root .local/runtime result RUN_ID
company-bid --root .local/runtime rerun RUN_ID --from-node N6
company-bid --root .local/runtime resume CHILD_RUN_ID
company-bid --root .local/runtime diff RUN_ID CHILD_RUN_ID
company-bid --root .local/runtime repair DAMAGED_RUN_ID

# 真实调用须先通过离线测试和样本核对。凭据只在本地未跟踪文件或环境变量中。
company-bid --root .local/live-runtime --config config/deepseek.example.json --credential-file .local/credentials/deepseek.key create --input .local/sample/prepared.json --stop-after N4
```

`result` 返回双 DOCX 的不可变路径/hash。`diff` 将受控对比文件写入运行目录，终端只输出其路径。输入文件/公司资料/预整理方案变化从 N0 建新 run；模型参数变化可从 N4 建子 run，复用仍有效的 N0。当前源码版本由实际源文件 hash 冻结，升级代码后旧 run 可能需从更早节点重算；不自动迁移旧成功记录。

`import_sources.import_directory` 提供离线原件导入，复制原件并按字节 hash 去重，保留用户授权、derived_from_bid 身份和文件名推定页范围。它是本地 catalog，**不是 PostgreSQL 入库**。不同字节的相似 PDF 不声称语义去重。现有用户授权不等于所有材料都经过现时有效期或公开登记复查。

## 验证与剩余边界

本地状态文件 + OS 锁只用于 M1；不宣称已具备 PostgreSQL 事务、租约/fencing、取消竞争、API 下载鉴权、三人并行、备份恢复或 Jaeger 导出补传。这些仍按 M2～M5 实施。

本轮源文核对限定于所选要求窗口，材料派生来源可追溯。N5 自动校验 schema/hash/引用/缺项，事实来源是否实质支持与方案是否充分仍需逐项源文核对；不会仅凭引用 ID 或模型 200 判定整份标书合格。

Word 正文与表格可编辑，证明附件为原件派生图像。目录/页码使用真实字段，未使用估算页码。Windows bundled runtime 没有 LibreOffice；本机 Word COM 启动失败，使用 WPS 原生只读导出 PDF，并接入 bundled `render_docx.py` 的栅格化步骤逐页检查。QA 不修改已发布 DOCX；下载后可在 Word/WPS 更新字段。辅助脚本为 `scripts/render_word_preview.py`。

回退：代码基线在 main，M1 在独立分支；原件不改。回退程序只切回已记录代码版本，不删除 `.local`、旧 run 或结果。不将不同执行版本下的检查点强行续跑；需要时建新分支 run。

## 初版记录（质量升级前，保留历史）

- 已导入 105 个来源文件，均为独立复制；11 项按策略跳过。源目录不写入，旧项目和 ai_bid 未连接。
- 真实样本：10 条要求、5 个用户授权公司事实、1 张招标固定表、1 项实际营业执照附件、2 个待补项。
- 离线测试：60 passed，1 skipped（当前 Windows 账号无法创建符号链接，保留未测事实）。包含真实子进程强杀、成功章节复用、N6 失败不重调模型、配置 fork、凭据/源文 canary、捕获故障、费用持久预留和受控差异报告。执行核心版本为 `876e14c`，完整源文件和依赖版本另外冻结于各 run 的节点版本。
- 真实执行 `8bffae29681446a3b390cbbee35448af`：先故意不加载凭据，N4 明确失败且 HTTP 请求为 0；同 run 加载凭据后只补失败的技术章，N0 hash 和公司事实单元保持，N4 成功暂停后跨进程继续 N6，最终 succeeded，不残留 paused_debug。
- 实际 DeepSeek 请求总计 **1 次**，输入 684、输出 1034，共 1718 tokens。耗时约 9.27 秒；按冻结高峰价估算 0.001446 美元，非账单确认。请求前预留上界 0.0062553 美元；没有付费自动重试。
- 审查发现原技术章有现场处置可能扩大合同范围的措辞，已基于记录响应离线局部修订，明确合同范围、业主授权、作业许可及确认责任。审校子 run `2aa18aa873274ebbbd06c002fb3e4bd8` **新增模型请求为 0**。其性质为 `replay_review` 文档原型，不冒称第二次模型生成或完整自动制作验收。原模型响应、父 run 和父 DOCX 均保留。
- 双 DOCX 为 7 页原型与 1 页待补清单；WPS 原生打开、字段更新、只读导出及逐页 PNG 检查已执行，原 DOCX hash 不变。普通段落/表格为可编辑 OOXML；未做用户交互式修改保存验收。报价表正常跨页，重复表头、6 个序号行与合计完整。
- 受控证据：`.local/evidence/m1/run-summary.json`、`review-revision.json`、`review-comparison.json`、`delivery-summary.json`，以及 `.local/real-sample/review.json` 的原文映射。原件/真实响应/完整诊断均不提交 Git；已验证凭据不进入 run JSON、日志、trace 和已跟踪文件。
- 本轮结果仍是所选要求范围内的 M1 原型，未宣称全 74 页覆盖、全部公司材料复审、完整标书或三人容量通过。

## 人工标书对标升级（当前结果）

2026-11-11，在旧设计复核后继续独立实现。功能提交 `af0fc77`，表头分页修复提交 `6bff64b`；没有复制旧项目源码、修改旧工作区或连接旧数据库。

- 内容：从授权人工标书准确摘取 12 个方法片段并绑定原生段落定位、原件及文本 hash。围绕 6 个技术模块建立 12 个稳定写作单元，逐项审查 55 个 must_cover 落点；涵盖设备巡检、干扰/排流、标准化与历史整改、培训及成果交付。采用 5 项已有公司事实，未新增未证实的人员、装备、历史业绩或报价。
- 表格与缺项：保留原招标报价表的字段、单位、6 行及合计合并，增加 3 张可编辑的拟用空白巡检记录表。新增表不是业主指定原表，也没有填入虚构测量数据。实际装配 1 项营业执照；4 项待补分别对应报价、签章、适用条款/参数/指定检查表、任务与历史整改资料交接。
- 样式：参考原稿采用 A4、2.5cm 页边距、宋体 12pt 正文、1.5 倍行距、两字符首行缩进、分级标题、封面/目录及连续页码。表格黑线、重复表头、可扩展行高；没有克隆整本原稿或复制历史签章。来源与制作说明移到清单追溯附录，正文保留应有缺项标记。
- 实际执行：`6a086d3d23a34dc5b658137f8cc9da9a` 从 N0 建新输入版本，N4 完成 21 个检查点（12 个写作单元、9 个章节装配）后暂停；另一个进程继续 N5/N6，最终 succeeded。该版本新增实际 HTTP 请求为 **0**，采集与 trace 状态完整。
- 排版修复：视觉检查发现一张记录表的首次表头与数据跨页分离。仅调整渲染器，创建 N6 子 run `8244e833c9a048368dd39de3aed51e6e`，N0/N4/N5 的输出路径与 hash 原样复用，实际只启动 N6。原成功版本保留，最终直接 succeeded，未重复生成正文或新增模型费用。
- 验证：完整离线测试 **79 passed、1 skipped**（Windows 符号链接权限），修复后运行耗时 14.65 秒。另由独立子 agent 审核精确写作合同和 11 项针对性测试；来源核对确认最终冻结输入、12 个摘录、5 个事实单元格、55 个映射及固定报价表一致。自动 coverage 仍只表示位置，不能替代这些实质核对。
- 文档：最终 Word 为 **16 页**；清单为 **6 页**，其中待补事项 2 页、范围与来源追溯 4 页。WPS 只读打开、更新字段、导出并逐页检查；修复前后仅正文第 12/13 页 PNG 变化，清单 6 页像素完全相同。原人工标书、父 run 和最终 DOCX 均核对 hash，QA 没有改写发布文件。

受控证据在 `.local/evidence/m1-benchmark/`；来源逐项复核在 `.local/benchmark-v2/source-final-review.json`，视觉复核在同目录的 `source-visual-review.json` 与 `source-final-visual-review.json`。最终文件路径/hash 由 CLI `result 8244e833c9a048368dd39de3aed51e6e` 返回。输入、全文、来源证据及文档均不进入 Git。

本轮是 **本地参考改编并经 AI 审校的所选范围原型**，新增 DeepSeek 请求为 0；不能据此称 DeepSeek 已自动达到同等质量。仍未覆盖全 74 页招标文件，未完成所有证明/人员/业绩装配，也未做用户实际编辑保存验收。下一步先用相同源文映射补足代表内容及验证窄单元真实生成质量，再按 M2 建立账号、本人项目权限、独立数据库和 Worker；不引入公司主体核对。
