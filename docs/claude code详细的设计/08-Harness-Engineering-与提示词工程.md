# Harness Engineering 与提示词工程

这篇文档聚焦一个问题：

> 为什么 Claude Code 的效果并不只是来自某条 system prompt，而是来自一整套“支架工程”。

这里说的 Harness Engineering，指的是 prompt 之外那层执行支架：system prompt 的拼装方式、动态上下文的注入方式、工具执行的并发模型、fork subagent 的缓存策略、session memory 的后台反思、prompt cache 的失效治理、compact 体系、权限与恢复逻辑。Claude Code 真正厉害的地方，在于这些东西是一起设计出来的。

## 一、什么叫 Harness Engineering

简单说：

- Prompt Engineering 关心“跟模型说什么”
- Harness Engineering 关心“模型处在什么运行时里被驱动、被约束、被补全、被恢复、被缓存”

Claude Code 在这方面最强的点是：它从来没有把“大模型很强”当作系统设计的替代品。它是把模型放进一个强约束、强状态、强恢复、强缓存感知的 runtime 里。

## 二、System Prompt 不是一段文本，而是可缓存、可分层、可失效治理的结构

### 1. 静态前缀与动态边界

`constants/prompts.ts` 明确把 system prompt 切成静态前缀和动态尾部，中间用 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 标记边界：

```ts
export const SYSTEM_PROMPT_DYNAMIC_BOUNDARY =
  '__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__'
```

主拼装顺序是：

```ts
return [
  getSimpleIntroSection(outputStyleConfig),
  getSimpleSystemSection(),
  getSimpleDoingTasksSection(),
  getActionsSection(),
  getUsingYourToolsSection(enabledTools),
  getSimpleToneAndStyleSection(),
  getOutputEfficiencySection(),
  ...(shouldUseGlobalCacheScope() ? [SYSTEM_PROMPT_DYNAMIC_BOUNDARY] : []),
  ...resolvedDynamicSections,
].filter(s => s !== null)
```

这意味着：

- 静态内容尽量稳定，尽量命中全局 prompt cache
- 动态内容放到边界之后，降低 cache prefix 碎片化
- “动态”不是随便拼，而是按 section registry 管理

### 2. System prompt section 有缓存策略

`constants/systemPromptSections.ts` 里，section 不是普通字符串，而是带缓存语义的对象：

```ts
export function systemPromptSection(name, compute) {
  return { name, compute, cacheBreak: false }
}

export function DANGEROUS_uncachedSystemPromptSection(name, compute, reason) {
  return { name, compute, cacheBreak: true }
}
```

这比大部分 agent 系统细很多。它在代码层区分：

- 可以缓存的 section
- 必须逐轮重算、并且会打断缓存的 section

也就是说，prompt 本身已经被当成一类“受性能约束的工程资产”来管理，而不是一坨字符串。

### 3. 为什么这很重要

大多数 agent 的 prompt cache 命中率差，不是因为模型不行，而是因为：

- agent list 在变
- MCP instructions 在变
- tool schema 在变
- current working directory / current status / plan reminders 在变

Claude Code 的做法是：把这些波动尽量移出 cache-critical prefix。下面这层 attachment lane，就是这个思路的直接体现。

## 三、动态信息尽量走 Attachment Lane，而不是污染 System Prompt

`utils/attachments.ts` 是这套系统的隐藏核心之一。它会在每轮前后把很多动态运行态注入为 attachment messages，而不是重写 system prompt。

典型注入内容包括：

- nested memory
- relevant memory surfacing
- changed files
- agent listing delta
- MCP instructions delta
- deferred tools delta
- todos
- plan mode reminders
- auto mode reminders
- pending teammate messages
- background task summary
- queued commands

这层设计的意义非常大：

1. 让 system prompt 更稳定。
2. 让动态信息有结构化类型，而不是无标签拼接文本。
3. 让每类运行态可以独立限流、独立裁剪、独立去重。

### 关键源码点

`memoryFilesToAttachments()` 会把 nested memory 注入成附件，并利用 `loadedNestedMemoryPaths` + `readFileState` 双重去重：

```ts
if (toolUseContext.loadedNestedMemoryPaths?.has(memoryFile.path)) {
  continue
}
if (!toolUseContext.readFileState.has(memoryFile.path)) {
  attachments.push({
    type: 'nested_memory',
    path: memoryFile.path,
    content: memoryFile,
  })
}
```

`getChangedFiles()` 会检查已读文件是否在模型看不见的情况下被外部修改：

```ts
const mtime = await getFileModificationTimeAsync(normalizedPath)
if (mtime <= fileState.timestamp) return null
```

这说明系统不仅在“给模型补上下文”，还在持续修正模型的世界模型。

## 四、QueryEngine 负责把一轮执行的“缓存安全前缀”拼好

`QueryEngine.submitMessage()` 不是简单把用户输入丢给模型。它会：

1. 构建 `toolUseContext`
2. `fetchSystemPromptParts()`
3. 拼 `defaultSystemPrompt` / `customSystemPrompt` / `appendSystemPrompt`
4. 注入 `userContext` 与 `systemContext`
5. 处理 orphaned permission
6. `processUserInput()`
7. 在第一轮模型响应前就持久化 transcript
8. 根据 slash command / user input 更新 permission context
9. 再进入 `query()`

可以把它理解为：它先组装“这一轮 query 的执行现场”，然后才调用主循环。

### 关键伪代码

```ts
async function submitMessage(prompt) {
  const { defaultSystemPrompt, userContext, systemContext } =
    await fetchSystemPromptParts(...)

  const processed = await processUserInput(...)
  this.mutableMessages.push(...processed.messages)

  if (persistSession) {
    await recordTranscript(messages)
  }

  updateToolPermissionContextFromUserInput()

  yield* query({
    messages,
    systemPrompt,
    userContext,
    systemContext,
    toolUseContext
  })
}
```

这里最关键的不是“调用 query”，而是 query 之前的每个准备动作都在服务于两个目标：

- 让模型拿到正确上下文
- 让系统保持可恢复、可缓存、可继续

## 五、queryLoop 才是真正的 Agent Kernel

`query.ts` 里的 `queryLoop()` 是全系统最核心的函数之一。它不是一个普通的 streaming wrapper，而是一套递归推进的 agent kernel。

### 这个 loop 在做什么

每一轮会做：

1. relevant memory prefetch
2. skill discovery prefetch
3. tool result budget
4. history snip
5. microcompact
6. context collapse
7. autocompact
8. model streaming call
9. streaming tool execution
10. tool use summary 异步生成
11. attachment refresh
12. recovery 或继续下一轮

### 接近源码的伪代码

```ts
while (true) {
  startRelevantMemoryPrefetch()
  startSkillPrefetch()

  messagesForQuery = applyToolResultBudget(messages)
  messagesForQuery = maybeSnip(messagesForQuery)
  messagesForQuery = maybeMicrocompact(messagesForQuery)
  messagesForQuery = maybeContextCollapse(messagesForQuery)

  const compact = await autocompact(messagesForQuery)
  if (compact) {
    state.messages = buildPostCompactMessages(compact)
    continue
  }

  const stream = callModel({
    messages: prependUserContext(messagesForQuery),
    systemPrompt,
    tools,
    thinkingConfig,
  })

  for await (const event of stream) {
    if (event.tool_use) {
      streamingToolExecutor.addTool(...)
    }
    yield event
  }

  if (!needsFollowUp) {
    if (recoverableError) {
      state = recover(state)
      continue
    }
    return completed
  }

  const toolResults = await executeTools(...)
  const attachments = await getAttachmentMessages(...)
  messages = [...messagesForQuery, ...assistantMessages, ...toolResults, ...attachments]
}
```

### 为什么这个 loop 比外界很多 agent 更稳

因为它已经显式编码了这些生产问题：

- prompt too long 之后怎么办
- max output tokens 打满之后怎么办
- tool 结果太大怎么办
- 工具还没跑完时能不能继续 stream
- background summary 能不能不阻塞下一轮
- compact 后怎样保留 plan / tail / API invariants

这不是 prompt 技巧，而是 runtime 设计。

## 六、工具执行不是“按顺序跑”，而是并发安全调度

### 1. `runTools()` 先分批

`services/tools/toolOrchestration.ts` 先把工具调用分成两类 batch：

- concurrency-safe 的读型工具，可以并发跑
- 非并发安全或会改状态的工具，必须串行跑

核心逻辑：

```ts
function partitionToolCalls(toolUseMessages, toolUseContext) {
  const isConcurrencySafe = tool.isConcurrencySafe(parsedInput.data)
}
```

这件事很重要，因为工具系统一旦扩大，不能再假设“所有工具都可以一起跑”。

### 2. `StreamingToolExecutor` 边流边跑

`StreamingToolExecutor` 进一步把工具执行推进到流式阶段：tool block 一边从模型流出来，一边就开始执行，而不是等整个 assistant message 结束。

它还处理：

- 并发控制
- sibling error abort
- streaming fallback synthetic errors
- user interrupt cancel / block
- ordered result emission

这意味着工具执行不是被动收尾，而是 agent loop 的并行子系统。

### 3. 为什么这比“工具调用后统一执行”强

因为它减少了：

- 模型流结束到工具启动之间的空窗
- 多个读型工具的串行等待
- 工具长尾造成的总 turn latency

而且它没有为了速度牺牲一致性，因为结果仍按顺序输出，context modifier 仍延后合并。

## 七、Fork Subagent 的真正厉害之处，是缓存安全复用

很多系统也会 spawn 子代理，但 Claude Code 的 fork agent 不只是并发，它是“缓存安全的上下文复制”。

### 1. `buildForkedMessages()` 用占位 tool_result 保持 prefix 一致

`tools/AgentTool/forkSubagent.ts` 的核心思路是：

- 保留父 assistant message 的全部内容
- 为所有 `tool_use` 块构造完全相同的 placeholder `tool_result`
- 只在最后追加每个 child 独有的 directive 文本

伪代码：

```ts
return [
  fullAssistantMessage,
  userMessage([
    ...placeholderToolResults,
    buildChildMessage(directive)
  ])
]
```

这样多个 fork child 的 API 前缀几乎字节级一致，最大化 prompt cache 命中率。

### 2. `CacheSafeParams` 明确把缓存关键项抽出来

`utils/forkedAgent.ts` 直接把 cache 关键项建模成结构：

```ts
type CacheSafeParams = {
  systemPrompt
  userContext
  systemContext
  toolUseContext
  forkContextMessages
}
```

这是很少见也很聪明的设计。它说明作者不是“希望子代理碰巧复用缓存”，而是显式围绕 cache key 设计 API。

### 3. `useExactTools` 保障 tool schema 一致

`tools/AgentTool/runAgent.ts` 里，fork child 会走 `useExactTools` 分支：

- 直接继承父工具集合
- 继承父 thinkingConfig
- 保持 querySource 语义
- 避免因为工具解析差异造成 prefix 变化

这本质上是在说：subagent runtime 也必须服务于 prompt cache。

## 八、Session Memory 是后台反思代理，不是普通摘要

`services/SessionMemory/sessionMemory.ts` 是另一个非常值得学习的点。

### 1. 什么时候触发

它不是每轮都跑，而是按阈值触发：

- 初始化阈值：context 足够长以后才启用
- 更新阈值：token 增量达到一定程度
- 工具调用数达到一定阈值
- 或者到达“自然停顿点”

### 2. 怎么触发

它注册在 post-sampling hook：

```ts
registerPostSamplingHook(extractSessionMemory)
```

### 3. 怎么执行

它不是主线程直接总结，而是：

1. 创建隔离的 subagent context
2. 读 session memory 文件
3. 构造 update prompt
4. `runForkedAgent(...)`
5. `canUseTool` 只允许对该 memory 文件执行 `Edit`

权限收敛是这样的：

```ts
if (tool.name === FILE_EDIT_TOOL_NAME && file_path === memoryPath) {
  return allow
}
return deny
```

### 4. 为什么这个设计好

因为它把“反思 / note taking / continuity”做成了后台低干扰子系统：

- 不阻塞主对话
- 不污染主上下文
- 能服务于 compaction
- 权限极小，风险可控

这比简单地让模型“每次都总结一下”高级很多。

## 九、Compaction 不是一次摘要，而是多级压缩策略

Claude Code 不是只有一个 compact 入口，而是分层压缩：

- tool result budget
- history snip
- microcompact
- context collapse
- autocompact
- sessionMemoryCompact

`services/compact/sessionMemoryCompact.ts` 甚至会优先利用 session memory 作为已抽取的工作记忆，再保留最近的 tail，并且显式修复 API invariants：

- 不拆开 tool_use / tool_result 对
- 不丢失 shared message.id 的 thinking blocks

这说明它非常清楚：compact 不是“压缩一下历史”，而是在保护一个仍然可继续执行的 agent state。

## 十、Prompt Cache Break Detection 让缓存不再是黑盒

`services/api/promptCacheBreakDetection.ts` 的设计很“工程师脑”：

- 记录 system hash
- 记录 tools hash
- 记录 per-tool schema hash
- 记录 cache control hash
- 记录 model / fastMode / betas / effort / extra body
- 生成 diffable content

也就是说，它不是“发现 cache miss 了很可惜”，而是会追问：

- 是哪一段 system prompt 变了
- 是哪个 tool description 变了
- 是不是 beta header 变了
- 是不是 MCP 工具的动态连接导致的

这就是 Harness Engineering 的典型思路：把抽象问题变成可测量、可归因、可修的工程问题。

## 十一、为什么 Claude Code 的 Harness Engineering 会让它写代码更强

最终回到用户感知层面，这些工程设计让它在写代码时表现出几个优势：

### 1. 它更像持续工作的工程师，而不是一次性补全器

因为它能记住：

- 已读文件
- 当前计划
- 已经完成的步骤
- 外部修改
- 最近压缩边界
- 子代理返回结果

### 2. 它更能从全局判断，而不是局部猜测

因为动态附件、git 状态、memory surfacing、changed files、todo、plan 都会持续修正它的世界模型。

### 3. 它更稳定

因为 prompt 太长、工具太多、输出超限、并发工具失败、背景任务中断这些问题，都不是交给模型自己解决，而是 runtime 有预案。

## 十二、给别的 AI 系统抄作业的 Harness Checklist

如果你在设计另一套 coding agent，下面这些是最值得抄的：

1. 把 system prompt 切成静态前缀和动态尾部。
2. 给动态 section 显式建缓存语义，不要所有内容都逐轮重算。
3. 把运行时波动尽量迁移到 attachment lane。
4. 让 tool runtime 区分并发安全与串行执行。
5. 支持 streaming tool execution，而不是流结束后统一执行。
6. 让 fork child 以 cache-safe prefix 共享上下文。
7. 把 session memory 做成后台、低权限、低干扰反思代理。
8. 把 compact 做成多级策略，不要只有一次摘要。
9. 对 prompt cache miss 做可归因检测。
10. 把权限、恢复、resume、transcript 视为 runtime 核心，不要视为外围功能。

## 十三、对应源码与深入阅读

- `constants/prompts.ts`
- `constants/systemPromptSections.ts`
- `QueryEngine.ts`
- `query.ts`
- `utils/attachments.ts`
- `services/tools/toolOrchestration.ts`
- `services/tools/StreamingToolExecutor.ts`
- `tools/AgentTool/forkSubagent.ts`
- `tools/AgentTool/runAgent.ts`
- `utils/forkedAgent.ts`
- `services/SessionMemory/sessionMemory.ts`
- `services/api/promptCacheBreakDetection.ts`

配套阅读：

- [23-核心AgentLoop详解.md](./23-核心AgentLoop详解.md)
- [24-关键提示词原文.md](./24-关键提示词原文.md)
- [06-上下文压缩、性能与提示缓存](./06-上下文压缩-性能与提示缓存.md)
