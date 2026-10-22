# colab_cli Web 化改造文档

把现有的 Python CLI 编排器（`run.py` / `remote_workflow.py`）改造成
「Nuxt 后台 + Python 作业服务」双层架构的设计与实施文档。

## 阅读顺序

| 文档 | 内容 | 读者 |
| :--- | :--- | :--- |
| [01-架构设计.md](./01-架构设计.md) | 分层、技术选型与理由、风险登记、部署形态 | 全员，先读 |
| [02-接口契约.md](./02-接口契约.md) | TaskEvent 事件协议、Runner API、Nuxt API | 前后端并行开发的依据 |
| [03-数据模型.md](./03-数据模型.md) | TypeORM 实体、状态机、写库归属 | 后端 |
| [04-前端规范.md](./04-前端规范.md) | CSS 命名规则、页面清单、Pinia 划分 | 前端 |
| [05-任务清单.md](./05-任务清单.md) | P0–P5 分阶段 checklist | 全员 |
| [06-目录结构与并行使用.md](./06-目录结构与并行使用.md) | 文件放哪、开发期如何不打断现有用法 | 全员，动手前必读 |
| [07-UI风格优化计划.md](./07-UI风格优化计划.md) | P6 样式层重构：设计 token、交互态、响应式 | 前端 |
| [08-PM2部署.md](./08-PM2部署.md) | PM2 常驻两个进程、日志落盘与轮转、重启注意事项 | 运维 |
| [09-科技感UI与动效整改方案.md](./09-科技感UI与动效整改方案.md) | 基于现有 UI 的视觉升级、逐页动效和实施验收方案 | 前端、设计 |

## 决策速查

| 项 | 决策 | 理由出处 |
| :--- | :--- | :--- |
| 前端框架 | Nuxt 3 | [01 §3](./01-架构设计.md) |
| 前端状态 | Pinia | [04 §3](./04-前端规范.md) |
| CSS 命名 | `container_页面名_功能名_位置名_别名` | [04 §1](./04-前端规范.md) |
| ORM / 库 | TypeORM + MySQL | [03](./03-数据模型.md) |
| 认证 | JWT（access 15min + refresh 存库） | [03 §2](./03-数据模型.md) |
| **Redis** | **不引入** | [01 §4](./01-架构设计.md) |
| Python 层 | 保留，升格为 FastAPI 作业服务 | [01 §2](./01-架构设计.md) |
| 进程守护 | PM2（`ecosystem.config.cjs`），与 systemd unit 二选一 | [08](./08-PM2部署.md) |
| 写库归属 | **只有 Nuxt 写 MySQL**，Python 不碰 | [03 §1](./03-数据模型.md) |
| Colab 私有 API | 收进 `colab_transport` adapter，可降级 | [01 §5](./01-架构设计.md) |

## 一句话架构

```
浏览器 ──JWT──> Nuxt 3 (Nitro) ──内部Token──> Python Runner ──> Colab VM
                    │                              │
                 TypeORM                      colab_transport
                    │                         (native | cli)
                  MySQL
```

Nuxt 是纯展示与管理层，随便重启；Python Runner 常驻，持有跑几小时的任务。
