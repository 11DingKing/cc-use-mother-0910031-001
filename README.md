# 未成年志愿者授权排班服务端

区级文博中心假期多展厅开放场景下的完整 Python 服务端：管理志愿者身份、
**监护授权版本**、培训资格、场次需求与候补顺序；确认排班时**原子占用名额**
并**固定当时授权**；撤回同意、跨馆调班、重复确认、并发候补递补均产生确定
结果；监护人只能看到本人关联记录；运营员可解释每个安排为何有效；重启后
可继续清理过期暂占。

- 纯 Python 3.11 标准库实现（`sqlite3` + `http.server`），**零第三方依赖**。
- 持久化：SQLite（WAL 模式，`BEGIN IMMEDIATE` 串行化所有写临界区）。
- 并发：`ThreadingHTTPServer` 多线程，所有名额变更在单条写事务内完成
  “读计数 → 判定 → 写入”，进程内加锁、进程间由 SQLite 保留锁串行化。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例（既有契约）。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/volsched/`：排班服务端
  - `db.py`：SQLite 建表与写事务（`BEGIN IMMEDIATE`）
  - `identity.py`：账号、志愿者、监护关系、场馆、场次、培训资格
  - `consents.py`：监护授权版本（签发 / 改版 / 撤回 / 覆盖判定）
  - `scheduling.py`：暂占、确认、候补 FIFO 递补、跨馆调班、过期清理、解释
  - `app.py`：HTTP 路由、Bearer 令牌、角色鉴权、监护人数据隔离
  - `__main__.py`：启动入口（启动即清理 + 后台周期清理）
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域确定性测试（含并发与重启恢复）、HTTP 端到端测试。

## 运行

```bash
PYTHONPATH=src python3 -m volsched --db ./volsched.db --port 8080 \
    --hold-seconds 120 --sweep-interval 30
```

首次使用先调用一次引导接口创建首个运营员账号（仅当库中无账号时可用）：

```bash
curl -s -X POST http://localhost:8080/api/setup/bootstrap \
    -H 'Content-Type: application/json' \
    -d '{"display_name":"运营甲"}'
# -> {"ok":true,"data":{"account":{...},"token":"..."}}
```

后续登录用 `POST /api/auth/token`（body：`{"account_id":"..."}`）换取
Bearer 令牌。除引导与换令牌外所有接口都需要 `Authorization: Bearer <token>`。

## 关键规则（确定结果）

### 监护授权版本（不可变、可审计）

- 监护人每次签发产生 `seq` 严格递增的新版本，旧有效版本自动 `superseded`。
- 撤回把当前版本置为 `revoked`（记录时间、原因），不删除；重复撤回确定返回
  `409 NO_ACTIVE_CONSENT`。
- 授权范围是场馆 ID 列表；任何关联监护人的有效版本覆盖服务场馆即满足。
- **确认排班时固定授权**：安排行复制当时的版本 ID/序号/范围/签发时间。
  事后撤回或改版不影响已确认安排；运营解释视图同时展示快照与版本当前状态。
- 暂占/候补时预检资格；确认时再次复检（防止暂占窗口内撤回或培训过期）。

### 名额占用与候补

- `POST /api/slots`：有余量 → 原子创建带 `hold_expires_at` 的暂占（默认
  120 秒）；满员 → 进入严格 FIFO 候补（`seq` 单调）。
- 候补队列非空时新请求一律排队，不得越过队头抢占释放出的名额。
- 确认不重复占名额（暂占时已计数）；**重复确认**确定返回
  `409 ALREADY_CONFIRMED`（幂等冲突，携带原确认时间与授权版本）。
- 取消确认 / 取消暂占 / 暂占过期 → 在**同一事务内**递补：取候补队头，
  校验通过则创建 `source=waitlist` 的暂占并标记 `promoted`；队头资格不满足
  则在队头停止，**不跳过**（补齐资格后由 `POST /api/sessions/{id}/promote`
  或下一次名额变化触发）。
- 数据库唯一索引兜底：同一有效安排、同一 waiting 候补各至多一条。

### 跨馆调班

- 仅运营员，仅对**已确认**安排；目标场次满员 / 时间冲突 / 授权不覆盖新馆 /
  培训缺失均在写入前判定，失败时原安排保持有效（确定返回对应 409 错误码）。
- 成功时：在单事务内创建新确认安排（`source=transfer`，按**当时**授权重新
  快照）、取消原安排、对原场次原子递补候补。

### 过期暂占与重启恢复

- 暂占用 `hold_expires_at` 持久化；服务启动立即执行一次清理
  （`startup_sweep`），后台线程按 `--sweep-interval` 周期清理。
- 进程崩溃时未提交事务随连接关闭回滚，无脏暂占；已落盘的到期暂占在下次
  启动/周期清理时释放并递补候补（`tests/test_api.py::RestartSweepTest` 与
  真实双进程冒烟均验证）。

### 权限与数据隔离

| 角色 | 能力 |
|---|---|
| `operator` 运营员 | 全部基础数据、报名、确认、取消、调班、解释、事件审计、手动清理 |
| `guardian` 监护人 | 仅能为**监护关系内**的志愿者签发/撤回授权；只读本人关联志愿者的安排与候补；不可报名、不可解释、不可调班 |
| `volunteer` 志愿者 | 仅能操作绑定到本人账号的志愿者档案 |
| `venue_manager` 场馆负责人 | 仅能管理所属场馆的培训、场次，查看所属场馆的安排、解释与事件 |

越权访问单条安排返回 `403 NOT_GUARDIAN / VENUE_OUT_OF_SCOPE`；列表接口
按身份在服务端过滤，不依赖客户端自觉。

## 接口一览

| 方法 路径 | 角色 | 说明 |
|---|---|---|
| `POST /api/setup/bootstrap` | 无（一次性） | 创建首个运营员并返回令牌 |
| `POST /api/auth/token` | 无 | 账号换 Bearer 令牌 |
| `POST /api/accounts` | 运营 | 建账号（role/display_name） |
| `POST /api/venues` | 运营 | 建场馆 |
| `POST /api/venue-managers` | 运营 | 绑定场馆负责人 |
| `POST /api/volunteers` | 运营 | 建志愿者（按出生日期自动判定未成年） |
| `POST /api/guardianships` | 运营 | 建立监护关系（监护人接口的数据边界） |
| `POST /api/training` | 运营/场馆负责人 | 登记培训资格 pending/passed/expired，可带 expires_at |
| `POST /api/sessions` | 运营/场馆负责人 | 建场次（容量、起止时间） |
| `POST /api/sessions/{id}/close` | 运营/场馆负责人 | 关闭报名：取消全部暂占与候补（已确认保留），此后不再递补 |
| `GET  /api/sessions/{id}/status` | 认证用户 | 容量、暂占/确认计数、候补队列与位次 |
| `POST /api/consents` | 监护人(本人关联)/运营 | 签发授权新版本（volunteer_id + venue_ids） |
| `POST /api/consents/revoke` | 监护人 | 撤回当前有效版本（重复撤回 409） |
| `GET  /api/guardian/consents` | 监护人 | 本人关联志愿者的全部授权版本 |
| `POST /api/slots` | 运营/志愿者本人 | 报名：暂占或入候补（可指定 hold_seconds） |
| `POST /api/assignments/{id}/confirm` | 运营/志愿者本人/监护人/场馆负责人 | 复检资格并固化授权（重复确认 409） |
| `POST /api/assignments/{id}/cancel` | 同上 | 取消并原子递补，返回 promotions |
| `POST /api/assignments/{id}/transfer` | 运营 | 跨馆调班，body 指定 target_session_id |
| `GET  /api/assignments/{id}` | 相关方（隔离） | 安排详情（含授权快照） |
| `GET  /api/assignments/{id}/explain` | 运营/场馆负责人 | 逐字段解释安排为何有效 |
| `GET  /api/assignments` | 认证用户（按角色过滤） | 可按 session_id / volunteer_id 查询 |
| `POST /api/waitlist/{id}/cancel` | 运营/本人/监护人 | 退出候补 |
| `GET  /api/guardian/waitlist` | 监护人 | 本人关联志愿者的候补中记录 |
| `POST /api/sessions/{id}/promote` | 运营/场馆负责人 | 手动触发一次 FIFO 递补 |
| `POST /api/maintenance/sweep` | 运营 | 立即清理全部过期暂占 |
| `GET  /api/events` | 运营/场馆负责人 | 审计事件流（可按 session/volunteer/assignment 过滤） |

错误响应统一为 `{"error":{"code","message","details"?}}`，冲突类为 HTTP 409。

## 验证

```bash
# 全部回归（契约 + 领域 + HTTP，共含并发争用与重启恢复用例）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json
```
