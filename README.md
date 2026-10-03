# 未成年志愿者授权排班

本项目维护未成年志愿者授权排班的领域约定、角色边界与样例数据，并提供完整的服务端实现，覆盖文博中心假期多展厅同时开放时的排班场景：志愿者身份、监护授权版本、培训资格、场次需求与候补顺序统一管理，确认排班时原子占用名额并固定当时授权。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/volunteer_scheduling/`：排班服务端（纯标准库，SQLite 持久化）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归测试 + 服务端确定性与接口测试。

## 服务端架构

- **`service.py`（领域核心）**：所有业务规则。写操作一律在 `BEGIN IMMEDIATE`
  事务中串行执行，因此并发确认、并发取消、候补递补的结果都是确定的；
  确认排班在同一事务内完成名额占用，并把当时的监护授权版本固定到排班记录上，
  同时保存校验快照（授权范围、培训资格、名额占用、年龄、来源）供事后解释。
- **`storage.py`（持久化）**：SQLite（WAL）。暂占、排班、候补、授权版本、
  审计事件全部落库，进程重启不丢状态。
- **`api.py`（接口）**：`http.server` 实现的 JSON 接口；身份经
  `X-Actor-Role` / `X-Actor-Id` 请求头传递，授权规则在服务层强制执行。
- **`__main__.py`**：启动入口，支持后台定时清理过期暂占。

### 关键规则

- **授权版本**：每名志愿者同一时间至多一个生效版本；发布新版本使旧版本
  `superseded`（不影响已固定的排班）；显式撤回（`withdrawn`）会级联取消
  固定该版本的生效排班，并在同事务内按序递补候补。
- **确认**：校验场次开放 → 志愿者未成年（以场次开始日计）→ 生效授权覆盖
  场次场馆 → 培训资格在场次开始时有效 → 名额充足；全部通过才原子写入。
  携带幂等键的重复确认返回首次结果，不重复占额。
- **候补**：`seq` 在场次内单调递增；名额释放（取消、撤回、暂占过期、调班）
  时按序逐条校验资格，不合格者标记原因跳过，直至名额占满或队列清空。
- **跨馆调班**：原排班取消与新排班确认在同一事务内完成，重新校验目标场馆的
  授权范围并固定当时的授权版本；任一步失败整体回滚，原排班不受影响。
- **暂占**：确认前的临时名额占用，带过期时间；过期后由清理器（启动恢复 +
  定时任务 + `POST /maintenance/reap`）置为失效并触发递补。
- **可见性**：监护人只能读取本人名下志愿者的授权、排班、候补与暂占；
  运营员可查看全部并通过解释接口说明每条排班为何有效；场馆负责人只读。

## 运行服务

```bash
PYTHONPATH=src python3 -m volunteer_scheduling --db data.db --port 8080
```

可选参数：`--hold-ttl`（暂占有效期秒数，默认 900）、`--sweep-interval`
（过期暂占清理间隔秒数，0 表示关闭后台清理；启动时总会先恢复清理一次）。

### 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/guardians` `/volunteers` `/venues` `/sessions` | 档案登记（运营员） |
| POST | `/volunteers/{id}/consents` | 发布监护授权新版本 |
| POST | `/consents/{id}/withdraw` | 撤回授权（运营员或本人监护人），级联取消并递补 |
| POST | `/volunteers/{id}/trainings` | 登记培训资格 |
| POST | `/sessions/{id}/holds` | 创建暂占（幂等键） |
| POST | `/sessions/{id}/confirmations` | 确认排班（幂等键，可带 `hold_id`） |
| POST | `/sessions/{id}/waitlist` | 加入候补 |
| POST | `/assignments/{id}/cancel` `/transfer` | 取消 / 跨馆调班（幂等键） |
| GET | `/assignments/{id}/explain` | 解释排班为何有效（含确认时快照） |
| GET | `/sessions/{id}` | 运营视角：名额、名单、暂占、候补、事件 |
| GET | `/guardians/{id}/view` | 监护人视角：仅本人关联记录 |
| POST | `/maintenance/reap` | 手动清理过期暂占 |

错误响应统一为 `{"error": {"code", "message"}}`，`code` 稳定
（如 `session_full`、`consent_scope`、`qualification_missing`、
`idempotency_conflict`、`hold_invalid`），调用方可据此做确定性处理。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
