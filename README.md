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

### 结案后补赔漏项复核

巨灾赔款到账后发现漏登受损物品时的补赔流程，分层实现：

- `supplement_rules.py`：金额与期限判断（纯函数）——结案 30 天窗口、累计补赔不超过原估损
- `supplement_store.py`：保存层——漏项申报与带原因的补赔版本，另存不改写原赔付记录
- `supplement_api.py`：接口编排层——角色、案件、待确认名额与金额规则
- `app.py` 仅做 HTTP 路由，调度页为 `static/index.html`

接口：

- `POST /api/supplements`：查勘员（`surveyor`/`adjuster`）在结案 30 天内提交漏项、估损金额和漏登原因；同一案件仅允许一项 `pending`
- `POST /api/supplements/confirm`：主管（`supervisor`）确认，另存一条带原因的补赔版本（v1、v2…），不修改 `claims.final_payout` 和原 `payments`
- `POST /api/supplements/return`：主管退回补件（需说明），查勘员补正后可重新提交
- `GET /api/supplements`、`GET /api/supplements/todos`：漏项/补赔版本查询与待办

金额规则：补赔累计（已确认版本之和）+ 本次金额超过原估损时，申报直接标记为「退回补件」并附剩余额度说明，不占用该案件唯一的待确认名额；退回后可按更正金额重报。`--seed` 演示数据会把 CLM-DEMO-001 结案并预置一条待主管确认的漏项。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整赔付流程、重复报案、乐观锁冲突、批量伪证识别和角色权限。

## 局限

认证依赖请求头；证据仅校验提交的 SHA-256，不实际保存附件；欺诈规则是原型规则而非精算模型；支付记录可审计，但不连接真实银行、保险核心或气象灾害数据源。
