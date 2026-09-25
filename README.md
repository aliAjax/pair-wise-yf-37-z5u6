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

## 解除观察判定

- 接触者可关联多条病例：创建时传`case_ids`（兼容单条`case_id`），随访中可用`link_case`动作追加关联并顺延`exposure_end`。
- 解除观察按全部关联病例判定：最近一次暴露结束（`exposure_end`，缺省等同`exposure_start`）满14天，且关联病例均为`recovered`或`closed`，且本人未报告症状（`report_symptoms`动作可登记症状和`symptom_onset`）。
- `GET /api/contacts`返回的每条接触者带`release`字段：是否可解除、阻塞原因`reasons`、最早可解除时间`earliest_release_at`；可用`?as_of=YYYY-MM-DD`指定判定日期。判定实时计算，关联病例转归变化会立即反映到列表结果。
- `complete_followup`仅在满足条件时执行（可传`as_of`），确认后记录`released_by`/`released_at`并保留`case_ids`等联系史。

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

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
