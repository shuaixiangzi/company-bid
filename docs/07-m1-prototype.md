# M1 文档原型

更新日期：2026-11-11。当前为 M1 实施与验收，真实结果登记见本页末尾。没有开始 M2 数据库/服务实现。

## 已实现范围

- `src/company_bid` 独立 Python 包，严格 PreparedBid/DocumentPackage 类型、来源 hash、事实与拟实施方案分开。
- 人工准确预整理输入 → N0 冻结 → N4 逐章写作 → N5 校验 → N6 Word/清单。N1～N3 自动解析/提取/匹配尚未实现，不伪装为自动完成。
- 本地 CLI 同一执行器支持完整运行、单步、同 run 续跑、新子 run 重跑、损坏 checkpoint 修复分支。页/窗口/章节可用同一个通用 unit 合同；本轮实际接入章节。
- 每个节点、单元和实际 HTTP 尝试留记录；真实 OTel SDK spans 持久落盘。完整输入输出和请求响应保存在受控产物，普通日志/trace 仅含白名单元数据。
- 成功章节 checkpoint 不因后续章节或渲染失败重新调用模型。程序、prompt、模型配置改变时创建子 run；配置消费关系决定可复用前缀。
- DeepSeek 只接收当前技术章及对应招标要求，输出限制为方案段落和步骤；不传整本人工标书或无关人员证明。事实/表格/附件由应用装配。
- 原件、节点输出及结果不可变；结果读取只返回已发布字节。捕获失败、部分捕获和进程强杀留下的 trace 缺口可见。
- 使用价格上界估算，在请求前持久预留费用；中断或用量未知时不自动返还预留。配置示例限制每次执行 1 个 HTTP attempt、每 run 最多 2 次实际调用和 0.02 美元预留上限。价格仅为冻结配置估算，最终费用以服务商账单为准。[核对的官方接口与价格](https://api-docs.deepseek.com/quick_start/pricing/)

## 本地运行

Python 3.12+。使用 `uv sync --extra test --frozen` 安装锁定依赖；`uv run pytest -q` 验证。运行目录为项目根，CLI 可用 `uv run company-bid`，或设置 `PYTHONPATH=src` 后 `python -m company_bid.cli`。

Windows 首次使用真实资料前执行 `scripts/protect_local.ps1`，将 `.local` 限制为当前账号、SYSTEM 和管理员。真实原件、资料清单、全部运行内容、密钥均留在 `.local`，已从 Git 排除。首版原型是受信任本机管理员 CLI，不提供团队用户鉴权接口。

```powershell
# 人工预整理输入先经过 PreparedBid 校验；离线方案按 section_id 存储。
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

## 本轮记录

- 已导入 105 个来源文件，均为独立复制；11 项按策略跳过。源目录不写入，旧项目和 ai_bid 未连接。
- 真实样本：10 条要求、5 个用户授权公司事实、1 张招标固定表、1 项实际营业执照附件、2 个待补项。
- 离线测试：57 passed，1 skipped（当前 Windows 账号无法创建符号链接，保留未测事实）。包含真实子进程强杀、成功章节复用、N6 失败不重调模型、配置 fork、凭据/源文 canary、捕获故障和费用持久预留。
- 实际模型调用及最终文档验证：待最终运行记录补充。
