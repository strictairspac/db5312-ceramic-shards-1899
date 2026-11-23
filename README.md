# 陶驿窑务·预检记录分片

加权 Rendezvous Hash 重算候选归属，结合每轮搬迁字节预算生成可执行计划。面向演示数据和本地复现，场景、操作剧本和业务边界见 [BUSINESS.md](BUSINESS.md)。

## 业务模块

- 窑口节点
- 预检批次
- 重分片预算
- 搬迁清单

## 本地运行

```sh
cp -n .env.example .env
docker compose up -d --build --wait
```

- 工作台 `http://127.0.0.1:18301`
- FastAPI 文档 `http://127.0.0.1:19301/docs`
- MySQL 8.0 端口 `127.0.0.1:17801`，账号 `app`，演示库 `workflow`
- `docker compose exec -T backend python -m scripts.seed_demo` 手动重跑演示数据
- `docker compose logs -f backend worker` 查看运行情况
- `docker compose down` 停容器并保留演示卷

### 演示账号

统一密码 `Demo-Walkthrough-2026`。

| 账号 | 角色 | 能执行的操作 |
|---|---|---|
| `operator` | 经办员 | 读取档案、创建实验输入、提交执行、导出结果 |
| `reviewer` | 复核员 | 读取、导出和签署结果 |
| `viewer` | 观察员 | 读取、导出已有结果 |

`SEED_DEMO=true` 启动时生成账号与一个业务样例。工作台新建档案会预填样例。可编辑表单中的数值、文本和结构化 JSON 后提交。业务说明中的原始文件是上游示例，演示数据通过表单录入。角色枚举位于后端 `src/constants/workflow.py` 与前端 `src/constants/workflow.js`。执行状态枚举也位于这两个文件，使用于队列、列表和执行详情。证据校验码是报告 JSON 按 UTF-8、键排序且省略多余空格序列化后的 SHA-256。

## 工程结构

| 服务 | 实现 |
|---|---|
| frontend | Nginx 与原生 HTML、CSS、JavaScript |
| backend | Python 3.11、FastAPI、SQLAlchemy |
| worker | Python 持久队列执行与 Redis 唤醒信号 |
| db | MySQL 8.0，持久卷保存档案、执行、证据及审计 |
| redis | Redis 7，追加日志卷保存队列唤醒信号 |

编辑 `backend/src/services/domain.py` 可修改试算方法与业务表单说明。API 路由、数据定义和页面位于 `backend/src/routes/`、`backend/src/models/` 与 `frontend/src/pages/`。开发时更新源文件后重新构建对应服务。

## 交班账同步接口

试算完成（报告）与复核签署（签字）在业务事务内同步登记到事务性发件箱，再按提交顺序补读到交班账。接口面向交班程序，使用 `X-Handover-Key` 请求头（值取自 `HANDOVER_KEY`，未配置时回落到 `AUTH_KEY`）；工作台登录账号也可调用。

| 方法与路径 | 作用 |
|---|---|
| `GET /handover/state?crew=<班组>` | 查询该班组已追到哪一笔：`last_seq`、`pending_count`、`caught_up`、最近一条已确认账。 |
| `GET /handover/pending?crew=<班组>&limit=<n>` | 只读预览该班组尚未确认的待同步记录，不推进游标。 |
| `GET /handover/ledger?limit=<n>` | 按记账顺序列出共享交班账（每个事件只记一次）。 |
| `GET /handover/crews/{crew}/ledger?limit=<n>` | 从某班组视角列出其已确认到的交班账。 |
| `POST /handover/sync`（JSON `{"crew": "...", "limit": 100}`） | 从上次确认处补读到位。幂等，可重试。 |

路径式等价接口：`GET /handover/crews/{crew}/state`、`POST /handover/crews/{crew}/sync?limit=<n>`。

保证：

- **同成同败**：业务写入与待同步记录同事务提交；交班程序停机时业务照常办单。
- **顺序**：试算完成先入报告，复核签署后入签字；账内按提交顺序排列。
- **不丢**：游标按提交顺序（`seq`）推进，先开始、后提交的办单不会被越过。
- **不重**：交班账每个事件只记一次，按 `outbox_id` 幂等；回执丢失后重试不会重复记；账写妥才推进游标。
- **互不影响**：每个班组各自推进游标，一个班组落后不阻塞其他班组。

`.env.example` 提供 `FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT`、`DB_PASSWORD`、`AUTH_KEY`、`HANDOVER_KEY`、`SEED_DEMO` 与 `NETWORK_SUBNET`。修改 `NETWORK_SUBNET` 可避开本机现有容器网段。所有发布的端口绑定本机回环地址。接入独立部署前需配置自己的凭据并按业务范围做独立验收。

[MIT License](LICENSE)
