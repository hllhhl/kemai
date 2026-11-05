# 20 个成熟 Agent 项目的机制与借鉴清单

> 调研方式：GitHub Search API 按 `AI agent framework` / `LLM agent` / `coding agent` /
> `autonomous agent` / `agent desktop app` / `agent mcp skills` 六个查询取 star 排序，
> 去重后 154 个 star>1000 的仓库，再按与本项目的相关性挑 20 个。
> star 数为 2026-11-05 通过 `/repos/{owner}/{repo}` 实测，未经人工取整。
>
> 「机制」一列只写读过 README 或源码结构的；标 `(据项目描述)` 的是仅从仓库简介判断，
> 尚未细读，落地方案前需要再核。

## 与本项目关系最近的四类

| 类别 | 项目 | 为什么相关 |
| --- | --- | --- |
| Agent 骨架（harness） | pi、learn-claude-code、open-cowork | 课脉已经是「模型 + 工具循环」，差别在骨架成熟度 |
| 技能体系 | superpowers、agent-skills、ECC | 课脉有 skills 但没有质量规范和组合方式 |
| 记忆与上下文 | claude-mem、ECC | 课脉有 longterm.py 文件记忆，缺自动捕获与压缩 |
| 交互与渲染 | open-cowork、orca、opencode | 工具调用怎么展示、多会话怎么管 |

## 20 个项目

| # | 项目 | ★ | 定位 | 可借鉴的机制 | 课脉状态 |
| --- | --- | --- | --- | --- | --- |
| 1 | obra/superpowers | 312692 | 给编码 agent 的完整开发方法论，由可组合 skills 构成 | **技能不是清单而是流程**：一组 skill + 一段初始指令，保证 agent 真的会按顺序用它们 | 待做：课脉 skill 目前只有 name/description，没有「什么时候该按什么顺序用」 |
| 2 | affaan-m/ECC | 297509 | agent harness 性能优化系统（skills / instincts / memory / security / research） | 「instincts」= 先于推理的默认反应，把常见判断固化成规则，省一次模型调用 | 部分：sentinel 的 allow/ask/deny 属于这一类，但没有按场景积累 |
| 3 | anomalyco/opencode | 224697 | 开源终端编码 agent | 多模型切换、session 持久化、LSP 集成（据 README/安装方式） | 已借鉴：会话与模型切换；LSP 不适用于课程场景 |
| 4 | DietrichGebert/ponytail | 184844 | 让 agent「像最懒的资深开发」那样思考 | 用一句人格约束显著减少过度工程 —— 提示词工程的有效样本 | 已部分：课脉 SYSTEM 里「能查就直接查」属同类 |
| 5 | langgenius/dify | 160447 | LLM 应用开发平台：工作流、RAG、agent、可观测性 | 节点化编排 + observability（接 Langfuse/Opik/Arize） | 不适用（课脉是单机单用户），但「可观测」= 课脉的 **audit 日志**已具备 |
| 6 | anthropics/claude-code | 154611 | 终端 agentic coding 工具 | **Skills 渐进加载**：SKILL.md 头部声明何时用，正文按需读；子 agent 隔离上下文；hooks | 渐进加载**已具备**（`skills.py` 清单只取 frontmatter，正文按需读）；本轮补了 `lint_skill` 质量检查。子 agent 隔离仍缺 |
| 7 | langchain-ai/langchain | 149070 | agent 工程平台（框架层） | 工具调用协议、结构化输出的成熟抽象 | 已具备：课脉用文本 JSON 协议，够用且更省 token |
| 8 | openai/codex | 133281 | 本地 coding agent（含 IDE/桌面/云三种形态） | 沙箱执行 + 审批策略分离；同一 agent 多端复用 | 已借鉴：sentinel 审批；沙箱暂不需要（课脉不执行任意命令） |
| 9 | **earendil-works/pi** | 120205 | 极简可扩展 agent harness：unified LLM API、agent loop、TUI、coding agent | **课脉的 `model-runtime/@earendil-works/pi-ai` 就是它的包**。它另有完整的 agent loop 与 skills/prompt 模板规范 | **应优先深入**：课脉手写了 agent 循环，可能与上游 loop 分叉；skills 规范也可直接对齐 |
| 10 | browser-use/browser-use | 120091 | 浏览器 agent | DOM 抽取 + 受限动作空间 + 视觉回退 | 待做（可选）：PPT 找课若要自动翻页核对，可借鉴动作空间约束 |
| 11 | stablyai/orca | 116041 | 多 agent 编队：每个 agent 独立 worktree，一处统一跟踪，可手机端监控 | **并行会话 + 统一状态面板**；每个任务独立工作区，互不污染 | 待做：课脉一次只能跑一个 agent run |
| 12 | addyosmani/agent-skills | 113368 | 生产级编码 agent 技能集 | 技能按「触发条件—步骤—验证」三段写，且每个都带反例 | 待做：课脉当前 12 个技能结构不统一 |
| 13 | thedotmack/claude-mem | 113245 | 跨会话持久记忆压缩系统 | 自动捕获会话 → 压缩 → 下次注入；而不是靠人工写记忆 | 待做：课脉 longterm.py 靠 agent 主动 `memory_write`，容易漏 |
| 14 | paperclipai/paperclip | 111256 | 管理工作中的 agent 的应用 | 多 agent 的任务派发与状态追踪（据描述） | 不适用（单用户学习场景） |
| 15 | bytedance/deer-flow | 85018 | 长时程 SuperAgent：研究 + 编码 | 长任务的规划—执行—复盘分层，避免单轮上下文爆掉 | 部分：课脉 max_turns=6 + fit_context，缺「阶段性复盘」 |
| 16 | lobehub/lobehub | 83991 | agent 编排与协作 UI | 会话/角色/模型的可视化组织 | 不适用（课脉 UI 已定形） |
| 17 | ruvnet/ruflo | 80924 | 多 agent 集群编排 | 群组间的任务分解与结果汇总（据描述） | 不适用 |
| 18 | shareAI-lab/learn-claude-code | 79918 | 教「harness engineering」的教材 | **核心论点：Agency 来自模型训练，产品 = 模型 + Harness。** harness 的职责是给模型合适的工具、上下文与刹车 | **直接对应课脉**：这解释了为什么该抄的是骨架而不是提示词 |
| 19 | unclecode/crawl4ai | 86933 | 面向 LLM 的网页抓取 | 抓取→清洗→结构化，专为喂模型设计 | 待做（可选）：PPT 找课的资料采集 |
| 20 | OpenCoworkAI/open-cowork | 2256 | 开源桌面 agent（Claude Cowork 的开源实现） | loop-guard 双层循环检测、ToolUse/ToolResult 分块渲染、PermissionDialog + 规则持久化、compaction | **已落地**：循环守卫（`note_call`）、步骤时间线、审批弹窗 |

## 本轮已落地

1. **循环守卫**（来自 #20 open-cowork 的 `agent-runner-loop-guard.ts`）
   - 完全相同调用连续 2 次提示、3 次拦下；单工具累计 6 次提示、10 次停
   - 实测触发场景：Agent 会把 `kemai.course_brief` 连查两遍
2. **工具调用分块渲染**（#20 的 `ToolUseBlock`/`ToolResultBlock` 思路）
   - 新建任务页现在逐步显示「调了哪个工具、返回什么」，而不是一行摘要
3. **＋菜单外点关闭 / Esc**（交互缺陷，不来自哪个项目，属于基础体验）

## 下一批要落的（按价值排序）

| 优先级 | 来自 | 具体改动 | 验收标准 |
| --- | --- | --- | --- |
| ~~P0~~ 已完成 | #12 agent-skills | 技能质量检查 `lint_skill`：缺 description / allowed-tools / 避坑段会被列出 | 21 个技能跑出 19 个问题，说明检查真的在起作用 |
| P0 | #18 learn-claude-code | 按「模型 + harness」重新审视 SYSTEM 提示词：只写工具契约与刹车，不写业务步骤 | 同一问题下工具调用次数不增加 |
| P1 | #13 claude-mem | 会话结束自动压缩成一条记忆，替代依赖 agent 主动 `memory_write` | 连续三次对话能记住第一次的结论 |
| P1 | #6 claude-code | 子 agent 隔离：长任务拆给独立上下文，只回传结论 | 上下文长度不再随轮数线性增长 |
| P2 | #11 orca | 允许多个 agent run 并行，各自独立 | 同时开两个任务互不影响 |
| P2 | #9 pi | 对照上游 agent loop 检查课脉的手写循环是否漏了它的边界处理 | 列出差异清单 |

## 关于 #9（pi）的特别说明

课脉的 `model-runtime` 依赖 `@earendil-works/pi-ai@1.0.4`，而 pi 仓库 12 万星、覆盖
「unified LLM API + agent loop + TUI + coding agent」。也就是说课脉**只用了它的 LLM 层，
agent 循环是自己重写的**。

这不是错，但意味着：上游在 loop 层踩过的坑（上下文压缩、工具结果截断、重试边界）
课脉要重新踩一遍。上面的循环守卫、`fit_context` 都是在补这一课。

值得投入一次「以 pi 的 loop 为基准做差异对照」——不是照搬（那是 TS 且面向编码），
而是把它处理边界的方式逐条对照到 `agent_runtime.py`。
