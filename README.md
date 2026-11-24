# 批次血缘核验台

农产品溯源专员处理质量守恒与召回闭包的本地工作台。样例、规则和复核记录都在项目内，不依赖外部服务。

## 启动

```bash
docker compose up --build
```

打开 `http://127.0.0.1:${PORT:-18520}`。默认端口见 `docker-compose.yml`。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

## 操作

1. 在左侧选阶段，查看采信、整理、计算、判定和对账结果。
2. 选择 `dirty` 样例观察坏行和隔离原因。
3. 换 `clean` 样例执行批次，填写复核意见后签发。
4. 下载证据 JSON，核对来源摘要、指标和签发版本。
5. 在「父批余量与签发」面板从样例登记父批，登记拆批/合批单并签发；更正父批实称量后观察缺口、停签范围与冻结凭证。

业务规则见 `docs/BUSINESS-RULES.md`，字段定义见 `docs/DATA-DICTIONARY.md`。

## 父批余量与签发 API

| 方法与路径 | 作用 |
|---|---|
| `POST /api/ledger/lots` | 登记父批 `{lot_id, actual_qty}`，幂等 |
| `POST /api/ledger/lots/{lot_id}/corrections` | 更正实称量 `{new_qty}`，版本加一并返回最新工作台 |
| `GET  /api/ledger/lots/{lot_id}/corrections` | 更正流水 |
| `POST /api/ledger/docs` | 登记拆批/合批单 `{doc_id, kind, lines:[{lot_id,qty}]}`，草稿不占量 |
| `POST /api/ledger/docs/{doc_id}/sign` | 原子签发占量；合批任一父批不足整体回滚；重复签发幂等 |
| `GET  /api/ledger/docs/{doc_id}/evidence` | 下载签发时冻结的凭证（更正后内容与摘要不变） |
| `GET  /api/ledger/workbench` | 父批实称量/已签/可用/缺口与单据 `signable`、`blocked_by` |
| `POST /api/ledger/seed` | 从当前样例的父批关系登记台账，幂等 |
