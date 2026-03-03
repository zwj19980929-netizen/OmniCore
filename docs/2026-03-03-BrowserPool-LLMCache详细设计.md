# Browser Pool 与 LLM Cache 详细设计
> 日期：2026-03-03
> 适用范围：OmniCore 当前本地单进程运行时
> 目标：把 Browser Pool 与 LLM Cache 从“已接入的优化能力”提升为“可长期演进的稳定基础设施”

---

## 1. 设计目标

这两项能力的共同目标不是单纯“提速”，而是：

- 降低重复初始化与重复推理的固定成本
- 在不破坏任务隔离性的前提下提高吞吐
- 让性能优化具备可观测、可调优、可回退的边界

它们都属于 Runtime 基础设施，不应把业务正确性建立在“命中缓存”或“成功复用浏览器”之上。换句话说：

- 不命中缓存时，系统仍然必须正确执行
- 不复用浏览器时，系统仍然必须正确执行

性能优化只能降低成本，不能成为正确性的前提条件。

---

## 2. 当前实现边界

### 2.1 Browser Pool 当前事实

当前代码中的 Browser Pool 位于 [browser_runtime_pool.py](/D:/zwj_project/OmniCore/utils/browser_runtime_pool.py)。

可以确定的事实：

- 按事件循环维护一个 `BrowserRuntimePool`
- 当前池键是 `(browser_name, headless)`，实际只用到 `("chromium", headless)`
- 每个池键当前只维护一个底层 `Browser` 实例
- 任务获取的是浏览器租约，真正的隔离仍靠每个任务自行创建 `BrowserContext`
- 空闲实例按 `BROWSER_POOL_IDLE_TTL_SECONDS` 回收
- 已有基础计数：`acquires / reuse_hits / launches / releases / cleanup_closes`

尚未实现但后续应补齐的点：

- 每个池键的显式容量上限
- 获取租约时的等待队列与超时
- 浏览器健康探测与熔断
- 更细粒度的分池策略（例如 `fast_mode`、代理、存储态）

### 2.2 LLM Cache 当前事实

当前代码中的 LLM Cache 位于 [llm_cache.py](/D:/zwj_project/OmniCore/core/llm_cache.py)。

可以确定的事实：

- 当前是单进程内存缓存
- 使用 `OrderedDict` 做近似 LRU
- 具备 TTL 过期与容量淘汰
- 当前主要缓存 URL 分析和页面结构分析
- 缓存键可包含：`namespace / normalized_url / task_signature / page_fingerprint / prompt_version / model_name`
- 已有基础计数：`hits / misses / sets / evictions / expirations`

尚未实现但后续应补齐的点：

- 命名空间级别的独立容量控制
- 主动失效策略（按 URL 前缀、按 Prompt 版本批量清理）
- 跨进程共享缓存
- 更严格的负缓存策略

### 2.3 当前已落地的第一阶段实现

截至 2026-03-03，代码里已经落地了这一阶段的核心能力：

- Browser Pool 增加了每池键浏览器数量上限：`BROWSER_POOL_MAX_BROWSERS_PER_KEY`
- Browser Pool 增加了每浏览器上下文并发上限：`BROWSER_POOL_MAX_CONTEXTS_PER_BROWSER`
- Browser Pool 增加了获取超时：`BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS`
- LLM Cache 增加了同键并发单飞等待：`LLM_CACHE_INFLIGHT_WAIT_SECONDS`
- LLM Cache 增加了命名空间级容量配额
- LLM Cache 增加了按命名空间、URL 前缀、Prompt 版本的主动失效接口

也就是说，当前实现已经从“单浏览器复用 + 单全局缓存”演进到了“多浏览器分配 + 配额化缓存”的第一版可治理形态。

---

## 3. Browser Pool 详细设计

### 3.1 抽象边界

Browser Pool 复用的是浏览器进程，不是任务上下文。

必须坚持的边界：

- 可以复用 `Browser`
- 不可以跨任务复用 `BrowserContext`
- 不可以默认共享登录态、Cookie、本地存储
- 不可以因为池化而降低任务隔离

这条边界比性能更重要。如果做不到，就宁可退回按任务启动浏览器。

### 3.2 建议的分池键

当前的 `(browser_name, headless)` 只够最小可用。

建议未来扩展为：

```text
(browser_name, headless, fast_mode, proxy_profile, storage_profile, locale_profile)
```

各字段含义：

- `browser_name`：当前是 `chromium`，未来可扩到其他内核
- `headless`：图形模式与无头模式不能混用
- `fast_mode`：是否启用激进拦截和快速策略
- `proxy_profile`：不同代理出口必须分池
- `storage_profile`：如果未来允许受控登录态，应按受控身份分池
- `locale_profile`：某些页面受语言环境影响时需要隔离

其中 `proxy_profile` 和 `storage_profile` 属于高风险维度。没有明确治理前，不建议开放自动复用。

### 3.3 池大小设计

当前每个池键只有一个浏览器实例。这对单用户、低并发场景够用，但在中高频任务下可能成为瓶颈。

建议引入两个明确参数：

- `BROWSER_POOL_MAX_BROWSERS_PER_KEY`
- `BROWSER_POOL_MAX_CONTEXTS_PER_BROWSER`

建议初始默认值：

- `BROWSER_POOL_MAX_BROWSERS_PER_KEY = 1`
- `BROWSER_POOL_MAX_CONTEXTS_PER_BROWSER = 4`

原因：

- 当前产品还是个人单机优先，先控制资源上限更稳
- Playwright 单浏览器多上下文已经能覆盖轻量并发
- 过早把浏览器实例数做大，收益未必高，但内存占用和失稳风险会先上来

一个务实的经验范围：

- 轻量抓取：每个浏览器 4 到 8 个上下文通常可接受
- 重交互页面：每个浏览器 2 到 4 个上下文更稳

如果没有更细的任务分级，就先按 4 作为上限。

### 3.4 并发控制

Browser Pool 本身不应单独决定全部并发，它应该和运行时并发控制共同生效。

当前系统里已经有：

- `MAX_PARALLEL_BROWSER_TASKS`

建议后续采用两层限流：

1. Runtime 层限制同批次浏览器任务数
2. Pool 层限制每个池键的浏览器数和每个浏览器的上下文数

获取策略建议：

- 有空闲可用上下文容量时，直接分配
- 无容量但可新建浏览器时，新建
- 达到池上限后进入等待队列
- 等待超过 `BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS` 后失败并回退

建议新增参数：

- `BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS = 10`

这样能把“过载”显式暴露出来，而不是无限等待。

### 3.5 回收与失效策略

当前只有“空闲 TTL 回收”。后续建议补成三类回收：

1. 时间回收
- 继续保留 `BROWSER_POOL_IDLE_TTL_SECONDS`
- 适用于短突发任务后的资源释放

2. 健康回收
- 浏览器断连
- 创建上下文失败
- 导航失败率连续超阈值
- 内部 API 抛出不可恢复异常

3. 压力回收
- 池内长期空闲且系统内存压力上升
- 某池键长时间低命中，可以主动收缩

建议新增计数：

- `failed_launches`
- `failed_context_creations`
- `forced_recycles`
- `queue_timeouts`

### 3.6 健康检查与降级

Browser Pool 不是必须成功的能力，失败时要能快速降级。

建议健康检查策略：

- 每次租约归还时，检查 `browser.is_connected()`
- 每次获取前清理失效实例
- 如果同一池键短时间内连续启动失败超过阈值，短暂熔断

建议新增参数：

- `BROWSER_POOL_CIRCUIT_BREAK_THRESHOLD = 3`
- `BROWSER_POOL_CIRCUIT_BREAK_SECONDS = 30`

熔断期间的策略：

- 直接跳过池化，按任务临时启动浏览器
- 同时记录降级日志，避免用户误以为池仍在正常工作

### 3.7 可观测性

除了当前已有计数，建议长期保留以下指标：

- 池命中率：`reuse_hits / acquires`
- 平均等待时长
- 峰值活动租约数
- 单任务平均冷启动时长
- 按池键的浏览器占用情况

关键验收不是“有池子”，而是：

- 重复浏览器任务的冷启动次数下降
- 平均完成时间下降
- 错误率没有明显上升

---

## 4. LLM Cache 详细设计

### 4.1 抽象边界

LLM Cache 只适用于“半稳定分析结果”，不适用于高时效、强个性化、强副作用决策。

优先适合缓存的内容：

- URL 分析结果
- 页面结构分析结果
- 页面元素定位建议
- 基于页面内容的结构化摘要

不建议默认缓存的内容：

- 带用户强个性偏好的最终回答
- 高风险权限判断
- 明显依赖实时外部状态的结论

### 4.2 缓存键设计

这部分是正确性的核心，缓存键必须体现“语义一致性”。

建议键结构保持如下维度：

```text
namespace + normalized_url + task_signature + page_fingerprint + prompt_version + model_name
```

每一项都不能随便省：

- `namespace`：区分不同用途，避免不同分析类型串结果
- `normalized_url`：同一页面的基础定位
- `task_signature`：同一 URL 在不同任务目的下可能需要不同分析
- `page_fingerprint`：页面结构变了就必须失效
- `prompt_version`：Prompt 升级后必须避免复用旧结果
- `model_name`：不同模型的输出风格和稳定性可能不同

最需要避免的错误是：

- 只按 URL 命中
- 忽略 Prompt 版本
- 忽略页面指纹

这三种都会直接产生“看起来命中了，实际语义已失效”的假命中。

### 4.3 命名空间与容量

当前所有缓存共享一个 `max_entries`。对于后续扩展，建议按命名空间做逻辑配额。

建议至少分为：

- `url_analysis`
- `page_analysis`
- `dom_locator`
- `content_summary`

建议策略：

- 全局上限仍保留，防止内存失控
- 命名空间上限做软限制，避免某一类结果把其他类结果挤掉

对个人单机场景，一个务实的初始范围：

- 全局 `LLM_CACHE_MAX_ENTRIES = 512`
- `page_analysis` 占比不超过 50%
- `url_analysis` 占比不超过 25%

### 4.4 TTL 与失效策略

缓存失效不该只靠单一 TTL，建议组合使用：

1. 时间失效
- `URL_ANALYSIS_CACHE_TTL_SECONDS`
- `PAGE_ANALYSIS_CACHE_TTL_SECONDS`

建议初始值：

- URL 分析：30 分钟到 2 小时
- 页面结构分析：15 分钟到 1 小时

当前默认值 1800 秒是偏保守、可接受的起点。

2. 结构失效
- 页面指纹变化直接视为新条目
- 对页面结构分析，这比单纯 TTL 更关键

3. 版本失效
- Prompt 改版后提升 `prompt_version`
- 模型切换后自动因为 `model_name` 失效

4. 手动失效
- 按前缀清理某个命名空间
- 按 URL 前缀清理某类站点
- 调试或故障恢复时清空全部

5. 容量淘汰
- 保留近似 LRU
- 明确记录淘汰计数，避免静默抖动

### 4.5 负缓存策略

不是所有失败都值得缓存。

建议只缓存“可确认的稳定失败”：

- 明确无法解析的 URL 模式
- 页面内容为空且指纹稳定

不建议缓存的失败：

- 网络超时
- 临时渲染失败
- 模型超时
- 速率限制

原因很简单：这些失败通常是瞬时的，缓存它们只会放大偶发问题。

如果后续要做负缓存，建议额外加：

- 更短 TTL
- 独立命名空间
- 明确失败类型标签

### 4.6 并发控制

当前缓存是线程安全的，但仍然可能遇到并发重复计算。

典型场景：

- 多个线程同时发现某个键未命中
- 然后同时发起同一类 LLM 分析

这会浪费成本。建议后续补“单飞”机制：

- 同一键第一次未命中时，登记为 `in_flight`
- 后续同键请求等待第一个请求完成
- 成功后共享结果
- 超时或失败后释放占位

建议新增参数：

- `LLM_CACHE_INFLIGHT_WAIT_SECONDS = 15`

这样能显著减少热点页面在短时间内的重复分析。

### 4.7 一致性与降级

缓存命中不能掩盖错误。

建议使用以下降级原则：

- 命中结果解析失败时，直接视为 miss
- 读取缓存抛错时，跳过缓存继续执行
- 写缓存失败时，不影响主任务

也就是说，缓存永远是“旁路优化”，不是主路径依赖。

### 4.8 可观测性

建议长期跟踪以下指标：

- 命中率：`hits / (hits + misses)`
- 命名空间命中率
- 平均条目存活时长
- 淘汰率
- 过期率
- 单飞等待次数

验收标准不应只看“命中率高不高”，还要看：

- LLM 分析调用次数是否实降
- 错误命中是否可控
- 页面变化后是否能及时自然失效

---

## 5. 并发与资源协同

Browser Pool 和 LLM Cache 不能各自孤立调优，需要一起看。

典型协同关系：

- 浏览器并发过高，会放大页面分析请求数，进而抬高 LLM 压力
- 缓存命中率提高，会减少页面分析调用，间接降低浏览器停留时长

建议遵循以下顺序调优：

1. 先看 LLM Cache 命中率是否足够低
2. 再看 Browser Pool 冷启动和复用率
3. 最后再放大浏览器并发

如果一开始就盲目提高浏览器并发，常见结果是：

- 页面抓得更快
- 但 LLM 分析排队更严重
- 整体任务时延不降反升

---

## 6. 推荐的下一步实现优先级

如果要继续完善这两块，建议顺序如下：

1. 为 Browser Pool 增加更细的健康检查和熔断
2. 为 LLM Cache 增加按命名空间的命中率与淘汰率统计
3. 补批量失效入口到运维/UI 页面
4. 最后再评估是否需要跨进程共享缓存

对当前 OmniCore 阶段，最值得优先做的是第 1 和第 2 条。它们能继续降低资源抖动，而且不会明显抬高主流程复杂度。

---

## 7. 风险提醒

这两项能力最容易出现的风险不是“优化不明显”，而是“优化做错导致错误复用”。

必须持续警惕：

- Browser Pool 误复用上下文，导致任务互相污染
- LLM Cache 误命中旧页面或不同任务语义
- 高并发下等待队列失控，导致看似没报错但整体卡死

所以设计上的底线应该始终是：

> 先保证隔离和正确性，再追求命中率和吞吐。
