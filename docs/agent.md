# OmniCore Agent 文档索引

本文档是智能体（Agent）相关设计、优化、架构文档的统一索引入口。

---

## 1. 当前架构

- [通用Agent当前架构说明](2026-03-04-通用Agent当前架构说明.md) — 系统定位、分层架构、数据模型、业务流
- [通用Agent当前版本收尾说明](2026-03-04-通用Agent当前版本收尾说明.md) — 本版改动、原因、解决的问题
- [通用Agent路线图状态](2026-03-04-通用Agent路线图状态.md) — 已落地能力、阶段状态追踪

## 2. 规划与决策

- [规划能力优化方案](2026-03-26-规划能力优化方案.md) — 规划流水线 7 项诊断 + P0/P1/P2 优化设计（2026-03-26）**已实施 (2026-03-26)**
  - P0-2.1 规划前可行性预检：新增 `core/plan_validator.py`，集成为 `plan_validator` 节点 (order=15)
  - P0-2.2 结构化失败上下文：Replanner 改用结构化 JSON 上下文，replan_history 扩展 failure_types/failure_layer/error_summaries
  - P1-2.3 统一 Replanner：删除 `_legacy_replanner_node_v2` 和 `_legacy_replanner_node`，修复变量引用 bug 和编码损坏
  - P1-2.4 Validator/Critic 职责明确化：标记 failure_source 区分结构性失败(validator)与语义性失败(critic)
  - P2-2.5 任务成本预估：TaskItem 新增 estimated_cost 字段，执行器按成本排序调度
  - P2-2.6 成功路径传入 Replanner：successful_paths 已包含在结构化重规划上下文中
  - P2-2.7 编码问题：随 legacy replanner 删除一并解决

## 3. 感知与执行

- [网页感知能力增强方案](网页感知能力增强方案.md) — 网页数据提取质量提升
- [Web-Agent架构落地方案](2026-03-17-Web-Agent架构落地方案.md) — BrowserAgent 三层架构设计
- [视觉模型优化计划](视觉模型优化计划.md) — 视觉模型集成与优化

## 4. 架构升级

- [架构升级集成计划](2026-03-19-架构升级集成计划.md) — StageRegistry、MessageBus 等升级集成
- [系统升级文档0227](系统升级文档0227.md) — 2月底系统级升级记录
- [BrowserPool-LLMCache详细设计](2026-03-03-BrowserPool-LLMCache详细设计.md) — 浏览器池与 LLM 缓存设计

## 5. 历史演进

- [通用Agent演进路线图](2026-03-03-通用Agent演进路线图.md) — 原始总路线图
- [通用Agent文档索引(旧)](2026-03-04-通用Agent文档索引.md) — 旧版文档索引与维护原则
- 阶段落地说明：[第一阶段](2026-03-03-通用Agent第一阶段落地说明.md) / [第二阶段](2026-03-03-通用Agent第二阶段落地说明.md) / [第三阶段](2026-03-03-通用Agent第三阶段落地说明.md)

---

> 新增文档时请同步更新本索引。分类不确定时放入「架构升级」或「历史演进」。
