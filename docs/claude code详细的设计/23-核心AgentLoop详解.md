# 核心 Agent Loop 详解

这篇只研究一个问题：

> 这个项目最核心的 agent loop 到底是怎么运转的？

如果只看表面，会觉得它只是：

```text
用户输入 -> 模型 -> 工具 -> 模型
```

但真实代码不是。它更接近这样：

```text
用户输入
  -> QueryEngine.ask()
  -> processUserInput()
  -> queryLoop()
      -> 上下文预处理
      -> memory / skill prefetch
      -> tool result budget / snip / compact
      -> 调模型
      -> 流式收集 assistant message / tool_use
      -> 流式执行工具
      -> 注入附件 / changed files / memory
      -> 生成下一轮状态
      -> while(true) 继续
```

所以这不是一个单次 completion loop，而是一个递归多轮状态机。

## 1. 先看最外层壳：`QueryEngine.ask()`

- 文件地址：`QueryEngine.ts`
- 关键函数：`ask()`

它做的事情可以概括成：

```ts
async *ask(prompt) {
  // 1. 准备固定上下文
  { defaultSystemPrompt, userContext, systemContext } =
    await fetchSystemPromptParts(...)

  // 2. 组 processUserInput 所需的上下文
  processUserInputContext = {
    messages: mutableMessages,
    readFileState,
    loadedNestedMemoryPaths,
    discoveredSkillNames,
    options: { tools, commands, model, mcpClients },
  }

  // 3. 把用户输入转成消息序列
  result = await processUserInput(...)
  mutableMessages.push(...result.messages)

  // 4. 用户消息先落 transcript
  await recordTranscript(mutableMessages)

  // 5. 发出 system init message
  yield buildSystemInitMessage(...)

  // 6. 进入真正的 query loop
  for await (message of query(...)) {
    mutableMessages.push(message)
    maybeRecordTranscript(message)
    yield message
  }
}
```

这层要点：

- `ask()` 不是核心 loop，只是 loop 的入口壳。
- 它的核心作用是把“会话状态”打包好再交给 `query() / queryLoop()`。
- 这里持有的关键状态有：
  - `mutableMessages`
  - `readFileState`
  - `loadedNestedMemoryPaths`
  - `discoveredSkillNames`

也就是说，loop 从第一轮开始就不是无状态的。

## 2. 真正的核心 loop 在 `query.ts` 的 `queryLoop()`

- 文件地址：`query.ts`
- 关键函数：`query()`、`queryLoop()`

从代码上看，`query()` 只是一个包装器，真正循环的是 `queryLoop()`：

```ts
export async function* query(params) {
  const consumedCommandUuids = []
  const terminal = yield* queryLoop(params, consumedCommandUuids)
  for (uuid of consumedCommandUuids) {
    notifyCommandLifecycle(uuid, 'completed')
  }
  return terminal
}
```

所以你可以把 `queryLoop()` 当成整个系统的“主心脏”。

## 3. `queryLoop()` 的状态对象长什么样

代码一开始就建立了一个跨迭代的 `state`：

```ts
let state: State = {
  messages: params.messages,
  toolUseContext: params.toolUseContext,
  maxOutputTokensOverride: params.maxOutputTokensOverride,
  autoCompactTracking: undefined,
  stopHookActive: undefined,
  maxOutputTokensRecoveryCount: 0,
  hasAttemptedReactiveCompact: false,
  turnCount: 1,
  pendingToolUseSummary: undefined,
  transition: undefined,
}
```

这里最关键的是几项：

- `messages`
  这一轮模型能看到的历史与附件。
- `toolUseContext`
  当前工具执行上下文，里面有权限、工具集合、readFileState、AppState 访问器等。
- `autoCompactTracking`
  记录当前是否做过 compact、compact 后经历了几轮。
- `maxOutputTokensRecoveryCount`
  控制输出截断后的恢复次数。
- `hasAttemptedReactiveCompact`
  防止 reactive compact 死循环。
- `pendingToolUseSummary`
  上一轮工具使用摘要的异步 promise。
- `turnCount`
  当前 loop 已经递归了多少轮。

这说明它的 loop 不是“拿 messages 调模型”这么简单，而是一个明确的状态机。

## 4. Loop 一开始先启动旁路任务，而不是直接请求模型

这段很关键：

```ts
using pendingMemoryPrefetch = startRelevantMemoryPrefetch(
  state.messages,
  state.toolUseContext,
)

while (true) {
  const pendingSkillPrefetch = skillPrefetch?.startSkillDiscoveryPrefetch(
    null,
    messages,
    toolUseContext,
  )
  yield { type: 'stream_request_start' }
  ...
}
```

也就是说，每个用户 turn 的主循环一开始，会先做两类异步预取：

- relevant memory prefetch
- skill discovery prefetch

这两类任务不会阻塞主流程，而是在后面适当时机被消费。

换句话说，这个 loop 一上来就已经在做“并行准备未来上下文”。

## 5. 模型调用前的预处理非常重，这才是核心竞争力

每轮真正发 API 前，`queryLoop()` 会做一整套上下文整形。

### 5.1 建 queryTracking

```ts
queryTracking = toolUseContext.queryTracking
  ? { chainId: same, depth: +1 }
  : { chainId: newUUID, depth: 0 }
```

这表示整个 query 链是带深度和 chain id 的，方便分析和子代理链路追踪。

### 5.2 取 compact boundary 之后的消息

```ts
messagesForQuery = [...getMessagesAfterCompactBoundary(messages)]
```

意思是：真正发给模型的，不一定是完整 transcript，而是 compact 后的有效尾部。

### 5.3 应用 tool result budget

```ts
messagesForQuery = await applyToolResultBudget(
  messagesForQuery,
  toolUseContext.contentReplacementState,
  ...
)
```

这一步的作用是：

- 对巨大 tool result 做预算约束
- 必要时用 replacement 记录替代完整内容
- 避免大日志/大文件读结果把 prompt 撑爆

### 5.4 snip / microcompact / context collapse / autocompact

主流程顺序大致是：

```ts
if (HISTORY_SNIP) {
  messagesForQuery = snipCompactIfNeeded(messagesForQuery)
}

messagesForQuery = microcompact(messagesForQuery)

if (CONTEXT_COLLAPSE) {
  messagesForQuery = applyCollapsesIfNeeded(messagesForQuery)
}

{ compactionResult } = autocompact(messagesForQuery, ...)
if (compactionResult) {
  messagesForQuery = buildPostCompactMessages(compactionResult)
  yield compact artifacts
}
```

这个顺序非常关键：

1. 先轻量 snip
2. 再 microcompact
3. 再 collapse
4. 最后才 full autocompact

这表明作者在尽量优先保留更细粒度的上下文，而不是上来就重摘要。

## 6. 真正的模型调用阶段：流式消费 assistant message

经过预处理后，才进入 `deps.callModel(...)`：

```ts
for await (const message of deps.callModel({
  messages: prependUserContext(messagesForQuery, userContext),
  systemPrompt: fullSystemPrompt,
  tools: toolUseContext.options.tools,
  signal: toolUseContext.abortController.signal,
  options: {
    model: currentModel,
    fallbackModel,
    querySource,
    agents,
    mcpTools,
    taskBudget,
    queryTracking,
    ...
  },
})) {
  ...
}
```

注意这里有几个关键点：

- `prependUserContext(...)`
  表示 `userContext` 不是 history 的一部分，而是每轮在 query 前再插进去。
- `fullSystemPrompt = appendSystemContext(systemPrompt, systemContext)`
  表示系统 prompt 和 system context 是组合后发给模型的。
- `tools`、`agents`、`mcpTools`、`taskBudget`、`queryTracking` 都是按轮带进去的。

也就是说，模型每一轮看到的是一个 runtime 重新构造的“工作快照”。

## 7. 流式阶段同时做三件事

在 `for await (const message of deps.callModel(...))` 里面，主 loop 实际上同时做三件事：

### 7.1 收 assistant message

```ts
if (message.type === 'assistant') {
  assistantMessages.push(message)
}
```

### 7.2 抓取 tool_use

```ts
msgToolUseBlocks = message.message.content.filter(c => c.type === 'tool_use')
if (msgToolUseBlocks.length > 0) {
  toolUseBlocks.push(...msgToolUseBlocks)
  needsFollowUp = true
}
```

`needsFollowUp = true` 就是“这一轮还没结束，后面还要跑工具并递归下一轮”的信号。

### 7.3 如果启用了 streaming tool execution，工具边流边跑

```ts
if (streamingToolExecutor) {
  for (toolBlock of msgToolUseBlocks) {
    streamingToolExecutor.addTool(toolBlock, message)
  }
}

for (result of streamingToolExecutor.getCompletedResults()) {
  yield result.message
  toolResults.push(normalizeMessagesForAPI(result.message))
}
```

这意味着它不是“等 assistant 完整输出完所有 tool_use 再统一执行”，而是可以流式接收、边到边跑、按顺序回流结果。

## 8. `StreamingToolExecutor` 是核心 loop 的第二颗心脏

- 文件地址：`services/tools/StreamingToolExecutor.ts`
- 关键类：`StreamingToolExecutor`

这个类的职责很明确：

- 维护 tool 队列
- 区分并发安全和非并发安全工具
- 允许并发安全工具并行
- 非并发工具独占
- 按工具原始顺序发回结果
- Bash 错误时中止同批兄弟工具

它的核心结构是：

```ts
class StreamingToolExecutor {
  tools: TrackedTool[]
  toolUseContext
  hasErrored
  siblingAbortController

  addTool(block, assistantMessage)
  processQueue()
  executeTool(tool)
  getCompletedResults()
  getRemainingResults()
}
```

### 8.1 `addTool()`

```ts
addTool(block, assistantMessage) {
  toolDefinition = findToolByName(...)
  isConcurrencySafe = toolDefinition.isConcurrencySafe(...)
  tools.push({
    id, block, assistantMessage,
    status: 'queued',
    isConcurrencySafe,
  })
  processQueue()
}
```

### 8.2 `processQueue()`

```ts
for (tool of tools) {
  if (tool.status !== 'queued') continue
  if (canExecuteTool(tool.isConcurrencySafe)) {
    executeTool(tool)
  } else if (!tool.isConcurrencySafe) {
    break
  }
}
```

### 8.3 `executeTool()`

```ts
async executeTool(tool) {
  markExecuting(tool)

  if (alreadyAborted) {
    emitSyntheticErrorResult(tool)
    return
  }

  generator = runToolUse(tool.block, ...)
  for await (update of generator) {
    if (siblingErrorOrUserInterrupt) {
      emitSyntheticErrorResult(tool)
      break
    }

    if (isBashError(update)) {
      siblingAbortController.abort('sibling_error')
    }

    if (update.message.type === 'progress') {
      tool.pendingProgress.push(update.message)
    } else {
      messages.push(update.message)
    }

    if (update.contextModifier) {
      contextModifiers.push(update.contextModifier)
    }
  }

  tool.results = messages
  tool.status = 'completed'
}
```

这个设计非常强，因为它把“工具执行”从简单的 sequential loop，升级成了一个可流式、可并发、带取消传播的执行器。

## 9. loop 的错误恢复机制也做成了状态跳转

`queryLoop()` 里最成熟的地方之一，是各种错误恢复不是散落在 if 里，而是通过 `continue` 回到 while(true) 顶部，形成显式“状态跳转”。

典型几类：

### 9.1 fallback model

```ts
if (innerError instanceof FallbackTriggeredError && fallbackModel) {
  currentModel = fallbackModel
  clear assistantMessages / toolResults / toolUseBlocks
  recreate streamingToolExecutor
  stripSignatureBlocksIfNeeded()
  yield warning("Switched model")
  continue
}
```

### 9.2 prompt too long

```ts
if (isWithheld413) {
  if (contextCollapse can recover) {
    state = { ..., transition: 'collapse_drain_retry' }
    continue
  }

  if (reactiveCompact can recover) {
    state = { ..., transition: 'reactive_compact_retry' }
    continue
  }

  surface error and return
}
```

### 9.3 max output tokens

```ts
if (isWithheldMaxOutputTokens(lastMessage)) {
  if (canEscalateTo64k) {
    state = { ..., maxOutputTokensOverride: 64k }
    continue
  }

  if (recoveryCount < limit) {
    state = {
      messages: [...messagesForQuery, ...assistantMessages, recoveryMetaMessage],
      maxOutputTokensRecoveryCount: +1,
      transition: 'max_output_tokens_recovery',
    }
    continue
  }
}
```

这说明它的主 loop 不是“出错就结束”，而是内建了多层 retry / recover 策略。

## 10. loop 什么情况下会“自然结束”

一个关键判断点在：

```ts
if (!needsFollowUp) {
  ...
  return { reason: 'completed' }
}
```

也就是说，只有当本轮 assistant 没有再发出 tool_use，loop 才进入“可能结束”的分支。

但即便如此，它在真正 return 之前还会再过一遍：

- prompt-too-long / media error recovery
- max_output_tokens recovery
- stop hooks
- token budget continuation

所以“无 tool_use”不等于立刻结束，只是进入“结束前检查区”。

## 11. 如果 assistant 发了 tool_use，loop 怎么进入下一轮

如果 `needsFollowUp === true`，则进入工具执行与回灌阶段。

### 11.1 跑工具

```ts
toolUpdates = streamingToolExecutor
  ? streamingToolExecutor.getRemainingResults()
  : runTools(toolUseBlocks, assistantMessages, canUseTool, toolUseContext)

for await (update of toolUpdates) {
  yield update.message
  toolResults.push(normalizeMessagesForAPI(update.message))
  if (update.newContext) {
    updatedToolUseContext = update.newContext
  }
}
```

### 11.2 追加附件

工具完成后，不会马上下一轮，而是先补附件：

```ts
for await (attachment of getAttachmentMessages(
  null,
  updatedToolUseContext,
  null,
  queuedCommandsSnapshot,
  [...messagesForQuery, ...assistantMessages, ...toolResults],
  querySource,
)) {
  yield attachment
  toolResults.push(attachment)
}
```

### 11.3 消费 memory / skill prefetch

```ts
if (pendingMemoryPrefetch settled and not consumed) {
  memoryAttachments = await pendingMemoryPrefetch.promise
  toolResults.push(...memoryAttachments)
}

if (pendingSkillPrefetch) {
  skillAttachments = await collectSkillDiscoveryPrefetch(...)
  toolResults.push(...skillAttachments)
}
```

### 11.4 生成下一轮状态并 `continue`

```ts
state = {
  messages: [...messagesForQuery, ...assistantMessages, ...toolResults],
  toolUseContext: updatedToolUseContextWithQueryTracking,
  autoCompactTracking: tracking,
  turnCount: turnCount + 1,
  pendingToolUseSummary: nextPendingToolUseSummary,
  maxOutputTokensRecoveryCount: 0,
  hasAttemptedReactiveCompact: false,
  transition: { reason: 'next_turn' },
}

continue // while(true) 下一轮
```

这才是这个系统的真正 loop 结构：

```text
assistant 发 tool_use
  -> 工具执行
  -> 注入新附件
  -> 合并回 messages
  -> 再次进入 while(true)
```

## 12. 这个 loop 为什么比普通 agent loop 强

普通 agent loop 往往是：

```text
while tool_calls:
  call model
  run tools
```

这个仓库的 loop 是：

```text
while true:
  1. 预取 memory / skills
  2. 预算化 tool result
  3. snip / microcompact / collapse / autocompact
  4. call model
  5. 流式消费 assistant message
  6. 边流边执行工具
  7. 处理 fallback / prompt-too-long / token-limit recovery
  8. stop hooks / token budget
  9. tool result 回灌
  10. 再注入 changed files / queued commands / prefetched memories / skills
  11. 生成下一轮 state
```

差距在于，后者不是“工具循环”，而是“受控状态循环”。

## 13. 如果你只想抓最关键的代码点，就盯住这些位置

- `QueryEngine.ts`
  - `ask()`
- `query.ts`
  - `queryLoop()`
  - `applyToolResultBudget(...)`
  - `deps.microcompact(...)`
  - `deps.autocompact(...)`
  - `deps.callModel(...)`
  - `handleStopHooks(...)`
  - `generateToolUseSummary(...)`
- `services/tools/StreamingToolExecutor.ts`
  - `addTool()`
  - `processQueue()`
  - `executeTool()`
  - `getCompletedResults()`
  - `getRemainingResults()`

把这些函数串起来，你看到的就不再是一个“聊天机器人带工具”，而是一套完整的 agent runtime 调度内核。
