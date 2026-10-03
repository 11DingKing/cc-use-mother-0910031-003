# 志愿服务积分结算

本项目维护志愿服务积分结算的领域约定、角色边界与样例数据，并提供完整的 Python 服务端：
以签到、场次状态、服务角色和审核记录为依据生成积分分录，自动识别学校/场馆重复报送，
封账后仅允许更正单调整，支持逐笔来源解释与审计复算。

## 核心机制

- **事件溯源 + 纯函数重放**：签到、场次状态、审核、裁决、申诉、撤销、更正单、开/封账
  全部是只追加的不可变事件（`src/points_ledger/events.py`）。台账由纯函数
  `projection.replay()` 按全局序号确定性重放生成，在线入账与审计复算共用同一份代码。
- **复式分录守恒**：每张分录含志愿者账户与系统积分基金账户两条对冲行，合计恒为零；
  冲回（`reversal`）、申诉恢复（`reinstatement`）、更正单（`correction`）都是新增的
  平衡分录，任意时刻 `Σ全部账户 = 0`，并以 SHA-256 哈希链串联所有分录。
- **重复来源待确认**：同一 `(场次, 志愿者)` 出现学校与场馆两条报送时自动成组挂起，
  组内已先行计提的积分同步冲回；裁决 `duplicate` 仅采信胜者，裁决 `distinct` 双双放行。
- **取消同步冲回**：场次取消时，该场次所有仍生效的计提逐笔全额红字冲回。
- **封账不可变 + 更正单**：封账定格快照，之后该期合计永不改变；冲回/补提进入当前开放
  期间并标注原始服务年度（跨年度补录 `late_accrual`）；更正单是封账后的唯一调整通道。
- **并发安全**：写命令在进程锁 + `BEGIN IMMEDIATE` 事务内"重放-校验-追加"，
  封账幂等，并发封账只产生一条封账事件。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/points_ledger/`：积分结算服务端
  - `events.py`：不可变事件与场次/来源常量
  - `projection.py`：纯函数重放、计分规则、复式分录、守恒约束
  - `store.py`：SQLite 只追加事件库（WAL、串行化写事务）
  - `service.py`：领域服务（命令校验、余额、逐笔解释、排名、审计复算）
  - `server.py`：零第三方依赖的 HTTP JSON API
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域规则（20 例）与 HTTP 端到端测试。

## 运行

```bash
# 启动服务（默认 0.0.0.0:8080，SQLite 事件库 points_ledger.db）
PYTHONPATH=src python3 -m points_ledger.server --port 8080 --db points_ledger.db
```

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/volunteers` `/sessions` `/reports` `/checkins` `/reviews` | 志愿者、场次、报送、签到、审核 |
| POST | `/sessions/{id}/complete` `/sessions/{id}/cancel` | 场次完成 / 取消（自动冲回） |
| GET  | `/duplicates/pending` | 重复来源待确认队列 |
| POST | `/duplicates/resolve` | 裁决 `duplicate`（指定 `winner_report_id`）或 `distinct` |
| POST | `/appeals` `/appeals/decide` | 申诉（duplicate/revocation/cancellation/review）与裁决 |
| POST | `/revocations` | 部分撤销（按比例红字冲回） |
| POST | `/periods/open` `/periods/close` | 开放结算期 / 封账（幂等） |
| POST | `/corrections` | 封账后更正单（唯一调整通道） |
| GET  | `/volunteers/{id}/balance` `/statement` | 余额与逐笔积分来源解释 |
| GET  | `/ranking?period=YYYY` | 结算期排名（挂起/冲回净额 0 不上榜） |
| GET  | `/conservation` | 全账户守恒与哈希链校验 |
| GET  | `/audit/recompute?period=YYYY` | 审计复算：独立重放并与封账快照比对 |

## 验证

```bash
python3 -m unittest discover -s tests -v     # 23 个测试
python3 -m compileall -q src tools tests     # 编译检查
python3 tools/check_contract.py domain/contract.json
```
