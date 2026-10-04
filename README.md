# 植物病虫害检疫与传播追溯

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8306`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8306
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点。

## 离线登记与对账

- 批次登记传 `"offline": true` 后进入 `offline_registered`，登记数据必须包含凭证号 `certificate_no`、查验人 `inspector`、现场结论 `site_conclusion` 和种植点 `facility_id`。
- `POST /api/reconciliation`：网络恢复后批量对账，请体 `{"items":[{"code","certificate_no","official_result":"matched|returned|revoked"}]}`，逐条返回结果，单条失败不阻断整批；也可对单个批次执行 `reconcile` 动作。
- 官方结论为 `returned`/`revoked` 时批次进入 `held`，所属种植点自动转为 `shipping_suspended` 停止调运；挂起期间不能创建新批次，也不能放行。
- `review` 动作把批次转入 `correction_pending`（人工复核只能留待修正）；只有官方再次对账为 `matched` 才能解除拦截，挂起批次清零后种植点自动恢复。

## 跨口岸凭证冲突

- `POST /api/certificate-revisions`：提交凭证修改 `{"certificate_no","port","facility_id","base_revision_id","changes","submitted_at"}`；同一基准版本被不同口岸（或不同种植点）修改时两版都保留并登记冲突，响应中 `"conflict": true`。
- `GET /api/certificate-conflicts[?certificate_no=...]`：列出冲突，两版按提交时间、再按种植点排序。

## 旧数据升级回填

- `POST /api/upgrade/backfill`：按批号回填凭证号，请体 `{"mappings":{"批号":"凭证号"}}`，返回 `updated`/`failed`；失败（批号不存在、重号、凭证号缺失）会写入可重试记录。
- `GET /api/backfill-failures[?status=pending]`：查看失败记录与重试次数。
- `POST /api/backfill-failures/<code>/retry`：按记录中的凭证号重试（也可在请体中带 `certificate_no` 覆盖）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
