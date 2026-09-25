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

## 接触者解除观察规则

接触者可同时关联多个病例（创建时用 `case_ids`，旧的单个 `case_id` 仍兼容；数据中保留每条关联的 `links` 暴露窗口与 `exposure_start/exposure_end` 总窗口）。满足以下**全部**条件，工作人员才能执行 `complete_followup` 解除观察：

1. 最近一次暴露结束起满 14 天观察期；
2. 关联的**全部**病例均已 `recovered` 或 `closed`；
3. 接触者本人未通过 `report_symptoms` 报告症状。

接触者的列表和详情会附带 `release` 计算结果：`eligible`、`blockers`（逐项阻塞原因）、`pending_cases`、`window_ends_at` 和 `earliest_release_date`（最早可解除时间；被待查病例或症状阻塞时为 `null`）。解除校验失败时动作返回 409 并在错误信息中写明原因和最早可解除时间。

相关动作：

- `link_case`：追加关联病例（可携带该病例的暴露窗口）；在 `identified`/`following`/`completed` 状态均可执行。已解除的接触者若因此不再满足条件，会自动下调回 `following`。
- `report_symptoms`：报告本人症状（必填 `symptoms`，可选 `symptom_onset`），状态进入/回到 `following`，症状未排查前不能解除。
- `case.reopen`：病例转归回退（已康复或关闭 → 调查中，需 `reason`）。所有因此丧失解除前提的已解除接触者自动下调回 `following`，写入 `resume_followup` 审计。

解除与下调都不会删除联系史：`links`/`case_ids` 始终保留，既往解除记录归档在 `release_history`，下调原因记录在 `release_revoked`。待办结果随病例转归联动，病例再次康复关闭后可重新确认解除。`complete_followup` 的数据可带 `as_of` 指定评估日期（默认当天）。

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
