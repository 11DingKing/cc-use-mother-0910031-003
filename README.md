# 志愿服务积分结算

年度权益兑换前，同一活动可能被**学校与场馆重复报送**，**取消场次的积分未冲回**，
直接汇总会让排名与兑换额度失真。本服务以**签到、场次状态、服务角色、审核记录**
为事实依据生成**不可变积分分录**，把重复来源先挂入**待确认队列**，封账后只能
用**更正单**调整，并允许审计人员对任一结算期做**独立复算**。

全程零第三方依赖，仅需 Python ≥ 3.11（HTTP 层基于标准库 `http.server`）。

## 设计要点

### 不可变分录 + 对冲记账

- 分录一经落账不可修改、不可删除；冲回、撤销、更正永远新增**红字/蓝字对冲分录**。
- 余额 = 已入账分录之和，任意时点都可以逐笔重放复现，不存在"改数"操作。
- 积分按整数落地：`积分 = 小时 × 10 × 角色系数`，结果 `ROUND_HALF_UP`
  （领队 1.5 / 讲解员 1.2 / 协助员 1.0），杜绝浮点误差。

### 重复来源先挂起，核对结论定案

- 同一 (志愿者, 场次) 出现两笔以上不同签到号即判定为疑点（跨渠道双报或同渠道重发）。
- 发现疑点时，该组现有积分立即以红字**临时挂起**（净额归 0），整组进入**待确认队列**，
  因此排名与余额绝不包含未核实积分。
- 核对结论有三种，落审核记录后把组内净额一次性结算到目标值，
  **与处理顺序、报送先后无关**：
  - `confirm`：确认其中一笔；
  - `reject`：全部驳回；
  - `keep_both`：经核实两笔是各自独立的服务，逐笔成立。
- 尚有未结案待确认项时**拒绝封账**，避免把不确定积分封进快照。

### 取消场次同步冲回

- 归属年度未封账：已发积分逐笔红字冲回，余额归 0。
- 归属年度已封账：逐笔生成更正单（纯红字）记入当前开放期，原年度快照不变。

### 封账与更正单

- 封账对该年度余额拍**不可变快照**；封账后普通分录一律拒绝。
- 封账后的调整只有一条入口——**更正单**（需审批人、原因）：
  - 红字 + 蓝字成对（`new_amount` 重记 / `new_amount=0` 纯冲销 / `additive_amount` 纯补记）；
  - 一律记入封账年度之后的开放期，原快照保持不变；
  - 红冲额以"当前在账剩余额"为界，构造时做净额守恒自检。
- 并发封账经条件变量串行化：首个请求真正封账，其余幂等返回同一张快照。

### 申诉、部分撤销、跨年度补录

- **申诉**：登记不产生积分，审批成立才补记；封存年度的成立申诉自动走蓝字更正单。
- **部分撤销**：撤销额 1..剩余可撤额，累计撤销永不超过原分录额；封存年度拒绝直接撤销。
- **跨年度补录**：场次显式设置早于补录年度的 `plan_year`，分录归属计划年度；
  计划年度已封账时自动转为蓝字更正单。

### 查询逐笔解释 + 审计复算

- `GET /ledger`、`GET /entries/{id}` 逐笔返回依据与证据链（签到号、场次、角色、
  小时、系数、取整、待确认单、原分录、审批人……）。
- `GET /periods/{year}/recompute` 从签到/场次状态/审核结论**独立重算**该结算期，
  与年度分录合计、封账快照双向核对，并复核全部更正单的红蓝算术与引用完整性。
- `GET /conservation` 做全局守恒自检（累计对冲不超额、快照恒等、更正单凭证齐备）。

## 目录

```
domain/contract.json          领域角色、状态、不变量契约
src/domain_contract/          契约读取与校验
src/points_ledger/
  models.py     枚举与不可变值对象（场次/签到/审核/分录/待确认/更正单/红冲）
  errors.py     业务异常
  storage.py    存储接口 + 线程安全内存实现（封账条件变量）
  engine.py     纯函数积分规则与分录构造（复算可直接重放）
  service.py    全部业务规则与守恒保障
  api.py        HTTP/JSON 接口
tools/check_contract.py       契约命令行检查
tools/smoke_scenario.py       端到端业务叙事冒烟
tests/                        契约回归 + 领域规则 + HTTP 集成测试
```

## 验证

```bash
# 单元 + 集成测试（34 个）
python3 -m unittest discover -s tests -v

# 编译
python3 -m compileall -q src tools tests

# 端到端业务叙事（重复报送→取消→撤销→申诉→并发封账→更正单→跨年度补录→复算）
PYTHONPATH=src python3 tools/smoke_scenario.py

# 领域契约检查
python3 tools/check_contract.py domain/contract.json
```

## 启动服务

```bash
PYTHONPATH=src python3 -m points_ledger.api --host 127.0.0.1 --port 8080
# 或安装后：points-ledger --port 8080
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sessions` | 登记场次（可设 `plan_year`） |
| POST | `/sessions/{id}/held` | 标记正常举办，触发计提 |
| POST | `/sessions/{id}/cancel` | 取消场次，同步冲回/追冲更正 |
| POST | `/checkins` | 录入签到报送（重复自动进待确认） |
| GET  | `/pending` | 待确认队列（`include_resolved=true` 含已结案） |
| POST | `/pending/{id}/resolve` | 核对 `confirm`/`reject`/`keep_both` |
| POST | `/appeals` · `/appeals/{id}/approve` | 申诉登记 / 审批 |
| POST | `/entries/{id}/reverse` | 部分/全额撤销 |
| POST | `/late-entries` | 跨年度补录一批 |
| POST | `/periods` · `/periods/{year}/close` | 结算期 / 封账快照 |
| POST | `/corrections` | **封账后唯一调整入口** |
| GET  | `/balance?volunteer_id=&year=` | 余额（含快照与封账后调整） |
| GET  | `/ledger?volunteer_id=&year=` | 逐笔账本（带证据链） |
| GET  | `/entries/{id}` | 单笔来源解释 |
| GET  | `/rankings?year=` | 排名（封存后以快照为准） |
| GET  | `/periods/{year}/recompute` | 审计复算 |
| GET  | `/conservation` | 全局守恒自检 |

角色取值：`领队` / `讲解员` / `协助员`；场次状态：`计划中` / `正常举办` / `取消`。

### 典型流程

```bash
# 1) 场次与双渠道重复报送
curl -s localhost:8080/sessions -d '{"session_id":"S1","activity_id":"ACT-A",
  "service_date":"2025-03-01","hours":2}' -H 'Content-Type: application/json'
curl -s localhost:8080/sessions/S1/held -d '{}' -H 'Content-Type: application/json'
curl -s localhost:8080/checkins -d '{"checkin_id":"K1","volunteer_id":"V1",
  "session_id":"S1","role":"讲解员","source":"school"}' -H 'Content-Type: application/json'
curl -s localhost:8080/checkins -d '{"checkin_id":"K2","volunteer_id":"V1",
  "session_id":"S1","role":"讲解员","source":"venue"}' -H 'Content-Type: application/json'
# -> result=pending，V1 余额被红字挂起为 0

# 2) 核对确认学校报送（2h×10×1.2 = 24 分）
curl -s localhost:8080/pending/P-xxxx/resolve -d '{"decision":"confirm",
  "chosen_checkin_id":"K1","reviewer":"审计员"}' -H 'Content-Type: application/json'

# 3) 封账（并发安全、幂等）
curl -s localhost:8080/periods/2025/close -d '{"closed_on":"2026-01-05"}' \
  -H 'Content-Type: application/json'

# 4) 封账后只能更正（红冲 24 + 蓝字 20，入 2026 开放期）
curl -s localhost:8080/corrections -d '{"year":2025,"volunteer_id":"V1",
  "entry_id":"E-...","new_amount":20,"reason":"时长复核","approver":"审计组",
  "as_of":"2026-02-01"}' -H 'Content-Type: application/json'

# 5) 审计复算任一结算期
curl -s localhost:8080/periods/2025/recompute
```

## 复算恒等式

```
权威积分(Y) = 分组事实 G            # 由签到×场次状态×角色，按待确认结论取有效签到
            + 申诉成立 A            # 审核裁决
            + 人工部分撤销 P        # 审核裁决（红冲为负）
            + 手工更正单 M          # 审批裁决

账侧(Y)     = 年度分录合计
            − 入在本年但归属他年的更正
            + 归属本年、在以后开放期入账的更正净额

封存年度额外要求：年度分录合计 ≡ 不可变快照。
```

`matches=true` 当且仅当上述差异为空、申诉凭证齐备、全局守恒自检通过。

## 错误码

| HTTP | error | 含义 |
| --- | --- | --- |
| 400 | `validation_error` | 参数非法、金额越界 |
| 404 | `not_found` | 场次/分录/待确认单不存在 |
| 409 | `conflict` / `period_closed` | 状态冲突、年度已封账 |
| 412 | `precondition_failed` | 封账前置不满足（如仍有待确认项）、未封账却开更正单 |
