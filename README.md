# 传染病暴发调查与接触网络

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8303`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8303
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `case`：病例和调查状态；`contact`：接触者随访。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `GET /api/situation`：调查态势结果（聚集分组、跨组风险路径、随访待办），按当前数据实时计算，新增或更新记录后立即反映。

## 调查态势

`GET /api/situation`返回：

- `clusters`：同地点且发病日期相隔不超过14天的病例归为一组（至少2例）。已排除（`excluded`）病例不参与分组，也不计入`risk_persons`。
- `risk_paths`：沿"病例—接触者"关系（接触者的`case_id`，以及接触者本人也登记为病例的`person_id`关联）找到的连接两个聚集组的最短路径；已排除病例仍保留在关系图中，路径可以经过它们。
- `overdue_followups`：状态为`following`且`due_at`已早于当天的接触者；`summary.overdue_count`即待办人数。

病例在`reported`或`investigating`状态可执行`exclude`动作（需`reason`字段，角色为admin或clinician）转为`excluded`。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
