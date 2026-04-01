# 扩展性、工具、技能与 MCP

## 核心判断

这个仓库的架构优势，不仅来自主循环本身，还来自它把“能力”抽象成了一套协议层。也就是说，它不是围绕某几个写死的工具构建，而是围绕“能力注册、发现、约束、执行、组合”构建。

关键代码主要在：

- `src/Tool.ts`
- `src/tools.ts`
- `src/commands.ts`
- `src/skills/loadSkillsDir.ts`
- `src/tools/SkillTool/SkillTool.ts`
- `src/tools/AgentTool/agentMemory.ts`
- `src/tools/AgentTool/runAgent.ts`

## 1. Tool 抽象不是薄壳，而是完整能力协议

`Tool.ts` 定义的并不是一个简单函数接口，而是一套很厚的能力描述，包括：

- schema
- validateInput
- checkPermissions
- concurrency
- readOnly
- interrupt behavior
- deferred loading
- `searchHint`

这意味着一个工具在系统里不只是“可调用函数”，而是带运行语义的对象。

这种设计非常值钱，因为它让上层 runtime 能够理解：

- 这个工具危险不危险
- 能不能并发
- 应不应该在工具搜索里暴露
- 输入怎么校验
- 适合哪类调用情境

## 2. `tools.ts` 是工具注册中心，也是能力装配层

`tools.ts` 不只是汇总工具列表，它还会结合这些因素做筛选和装配：

- environment / feature gate
- deny rules
- simple mode
- REPL mode

也就是说，系统里的工具集合不是固定不变的，而是会根据当前环境和模式被裁剪。

这对扩展性非常关键。因为生产系统里最忌讳的就是“所有能力永远全部可见”，那会带来：

- 上下文膨胀
- 错误暴露
- 权限失控

## 3. Command 系统和 Tool 系统是并列而非混用的

`commands.ts` 的存在说明架构上明确区分了：

- 本地交互命令
- 模型可调用工具

这很成熟。因为 command 更像 UI/交互协议，tool 更像 agent 能力接口。把二者分开，能避免：

- 用户命令和模型动作混淆
- 权限语义混乱
- 路由层职责不清

## 4. Skills：高阶策略被对象化

`skills/loadSkillsDir.ts` 很值得关注。它从多个来源加载 skills：

- user
- project
- policy
- plugin
- bundled
- MCP

并且解析 frontmatter，包括：

- description
- whenToUse
- allowed-tools
- hooks
- agent
- effort
- model
- paths

这意味着 skill 在系统里不是一段静态 prompt，而是一个高阶能力对象，既描述：

- 什么时候该用
- 可以用什么工具
- 推荐用哪个 agent / model

又保留足够的扩展元信息。

## 5. SkillTool：把策略执行和子代理执行打通

`SkillTool.ts` 的价值在于，它能把 skills 真正跑起来，而不是只让模型“读一遍 skill 文本”。它还和 telemetry、MCP skills、forked subagent context 打通。

这说明 skill 的本质是：

- 可发现
- 可引用
- 可执行
- 可追踪

相比之下，很多外部系统的 skill 只是 prompt preset，层级明显更低。

## 6. MCP 接入方式说明它在做开放能力底座

从技能加载和 agent 运行逻辑能看到，这个项目并没有把外部能力写死在内部实现上，而是在给 MCP 这样的外部能力源留正式接口。

这件事的价值在于：

- 新能力不一定要进主仓库
- 能力可以来自插件或远端 server
- skill、tool、resource 可以统一进入路由层

这比“加一个新工具就改一遍主 prompt”高级很多。

## 7. Agent Memory 和 Agent-specific MCP：说明扩展不是平铺，而是按代理分层

`agentMemory.ts` 和 `runAgent.ts` 还能看到两件重要的事：

- agent 可以有 user/project/local 级别记忆范围
- agent 可以附带 agent-specific MCP servers

这意味着系统开始支持“不同代理拥有不同能力边界和不同知识边界”。

这是平台级架构非常重要的一步，因为它允许：

- 研究代理和执行代理不同配置
- 只读代理和写代理不同权限
- 某类专用代理只加载相关 MCP 能力

## 8. 这套扩展架构为什么有竞争力

### 8.1 上层面向能力，不面向实现

只要遵守 Tool / Skill / MCP 的协议，能力就可以被接入，而不必重新改主循环。

### 8.2 能力发现与能力执行分离

tool search、skill discovery、实际调用是不同层次。这样才能在能力变多时保持稳定。

### 8.3 策略层和动作层分开

skill 更像策略模板和工作流封装，tool 更像原子动作，agent 更像执行容器。分层很清楚。

## 9. 结论

如果把这个项目看成一个 agent 产品，它厉害的不是工具多，而是它有一套比较完整的能力协议层。这样它未来加新模型、新工具、新插件、新 MCP server 时，不需要推倒主架构。

这也是它具备平台潜力的重要原因。
