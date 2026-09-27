# 巨灾保险理赔调度系统

标准库实现的巨灾理赔受理、分级、查勘、复核、紧急预付和最终核定服务，数据保存到 SQLite。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8207`，默认数据库 `catastrophe_claims.db`。可通过 `--db`、`--host`、`--port` 修改。

## 主要接口

请求头 `X-User`、`X-Role` 表示用户与角色。角色有 `intake`、`adjuster`、`surveyor`、`supervisor`、`auditor`。

- `GET /health`、`GET /api/state`、`GET /api/queue`
- `POST /api/claims`：创建报案并识别重复报案
- `POST /api/claims/triage`：计算优先级和欺诈风险
- `POST /api/claims/assign`：分配查勘人员
- `POST /api/evidence`：添加证据并识别跨案件批量复用
- `POST /api/claims/survey`、`POST /api/claims/submit-review`
- `POST /api/claims/emergency-advance`：仅限监督人员、紧急且未超20%的案件
- `POST /api/claims/finalize`：锁定最终核定结果

## 结案后漏项补赔复核

巨灾赔款到账后发现漏登受损物品时，走独立的补赔版本流程，**原赔付记录不改写**。四层分开实现，均为标准库：

- `supplement_rules.py`：纯函数规则（金额判断、30天时效），无 IO
- `supplement_store.py`：持久化层（`supplements` 表、部分唯一索引、查询）
- `supplement_service.py`：业务编排（提交、确认、拒认、待办）
- `supplement_api.py`：HTTP 路由，由 `app.py` 挂载
- 调度页：`/supplements`（`static/supplements.html`），可看到原案、漏项、补赔金额和待办

规则：

- 查勘员（`surveyor`/`adjuster`）在案件结案（`approved`/`closed`）后 **30 天内**提交漏项描述、估损金额和漏登原因，逾期 409。
- 同一案件仅允许一项 `pending`（数据库部分唯一索引 `uq_supplements_one_pending` 兜底）；主管确认或拒认后才能再报。
- 累计补赔不得超过**原估损** `claims.estimated_loss`。提交时超额则状态为 `returned`（退回补件，不占待办名额），可核减后重新提交；主管确认时再次校验，支持按剩余额度部分确认。
- 确认时另写一条 `payments(kind='supplement')`，补赔版本（`supplements` 行）保存原估损、原赔付、已累计补赔、原因和确认信息；`claims.final_payout`、案件状态与原付款行均不变。

接口（请求头同样使用 `X-User`、`X-Role`）：

- `POST /api/supplements`：查勘员提交漏项，body：`claim_id`、`item_description`、`estimated_amount`、`reason`
- `GET /api/supplements`：补赔版本列表，支持 `?claim_id=`、`?status=pending|returned|confirmed|rejected`
- `GET /api/supplements/todo`：按角色返回待办（主管看待确认，查勘员看待确认/退回）
- `GET /api/supplements/<id>`：单条补赔版本（含原案字段）
- `POST /api/supplements/<id>/confirm`：主管确认，body 可选 `confirmed_amount`（缺省按估损全额）、`review_note`
- `POST /api/supplements/<id>/reject`：主管拒认，body：`review_note`（必填）

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别和角色权限，以及补赔漏项的30天窗口、单案待办唯一、累计超额退回、部分确认、权限和原记录不改写。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
