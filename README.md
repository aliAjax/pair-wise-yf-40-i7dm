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

- `consignment`：检疫批次；`facility`：温室、苗圃或下游种植点；`credential`：官方凭证。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 离线登记与官方对账

各口岸的检疫批次先离线登记，回到网络后再与官方凭证库对账。

- 批次登记（`consignment`的`register`动作）记录凭证号、查验人、现场结论和种植点。
- 对账（`POST /api/consignments/<id>/reconcile`）按凭证号核对官方状态：凭证有效则标记`reconciled`；凭证不存在则保留为待处理。
- 官方退回（`credential`的`return`动作）或吊销（`revoke`动作）时，该凭证下的批次及其种植点停止调运（`shipment_stopped`与`official_hold`置位）。
- 人工复核（`POST /api/consignments/<id>/correct`）只能留下修正意见，不能清除官方拦截；被拦截批次的`release`等调运动作会被拒绝。官方解除（`lift`动作）才可恢复。

## 凭证并发修改与冲突

两个口岸同时修改同一凭证时，后提交的一方若版本已过期，不会被直接拒绝，而是保留两版并列出冲突：

- `POST /api/credentials/<凭证号>/modify`：提交`{"...":"...","port":"口岸","submitted_at":"...","expected_version":数字}`。版本过期时返回`conflict`并保留两版（各自带提交时间与种植点）。
- `GET /api/conflicts?status=open`：列出未解决的冲突。
- `POST /api/conflicts/<id>/resolve`：提交`{"pick":"a"|"b"}`选定保留版本。

## 凭证号回填（升级）

旧批次没有凭证号，升级时按批号回填：

- `POST /api/upgrade/backfill`：对缺少凭证号的批次按批号查找凭证并回填；找不到凭证的留下可重试记录。
- `GET /api/backfill?status=pending`：列出待重试的回填记录。
- `POST /api/backfill/<id>/retry`：重试指定的回填记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

植物检疫结论和传播链规则是流程演示，不替代法定检疫标准或实验室鉴定。
