# 行业对标发布管控系统（benchguard）

防止"几份公开样本数/失效数拼起来推知单件情况"的发布管控小型全栈系统。
**零第三方依赖**：仅用 Python 3.11 标准库（`http.server` + `sqlite3` + `threading`）与原生 HTML/CSS/JS，无需安装、无需构建。

## 管控规则与实现映射

| 业务要求 | 实现方式 |
|---|---|
| 从同一冻结批次选件 | 全系统一个冻结批次（`meta.frozen_batch_id`），申请写入前外键+批次校验，批次外件整单拒收 |
| 按历次发布与本次申请的入选组合分组 | 每件生成 0/1 入选向量，维度为全部历史发布（含已撤下）+ 本次申请（`benchguard/privacy.py`） |
| 除从未入选组外，任一组不足 5 件拒发 | `K_ANONYMITY=5`；全零向量组豁免，其余组 `<5` 即整单拒发，拒发原因按发布编码描述组合，不含部件身份 |
| 并发申请逐份裁决 | HTTP 只登记 `pending`；单守护线程 FIFO 串行裁决，SQLite `BEGIN IMMEDIATE` + 进程锁 |
| 放行登记与公开数字同时生效 | 批准时 `releases`/`inclusions` 与申请状态在**同一事务**提交；公开数字由 `inclusions` 同表派生 |
| 已撤下的仍参与核对 | 撤下只改 `releases.status`，`inclusions` 行保留，历史向量维度不删除 |
| 中断后恢复待办 | 启动时 `processing → pending` 重置，重新入队裁决 |
| 同一申请重试不重复发布 | `(applicant_id, idempotency_key)` 唯一约束，重复提交原样返回原申请 |
| 页面可申请、撤下、查看拒发原因 | 原生 JS 单页：申请页、我的申请（详情/拒发原因/撤下） |
| 部件名单只供机务核验 | 服务端角色强校验（非前端按钮显隐）+ 审计留痕，申请单位/匿名均 403/401 |

## 快速启动

```bash
cd benchguard
python3 -m benchguard
# 访问 http://127.0.0.1:18216
```

环境变量（均有默认值）：`BENCHGUARD_DB`（默认 `./data/benchguard.db`）、`BENCHGUARD_HOST`、`BENCHGUARD_PORT`、`BENCHGUARD_SEED`。
首次启动幂等写入种子：冻结批次 `FB-2026-Q4`、40 件部件、3 份历史发布（R-0003 已撤下，用于演示撤下仍参与核对）。

### 演示账号

| 账号 | 口令 | 角色 | 权限 |
|---|---|---|---|
| maintainer | `Mech123!` | 机务核验 | 部件名单、撤下、审计轨迹、申请全量查看 |
| applicant | `Apply123!` | 申请单位 | 提交/撤下自己的发布、查看自己的申请与拒发原因 |

公开对标数字页匿名可访问；部件名单任何非机务身份都被服务端拒绝。

## 典型验证路径

1. applicant 提交单件申请 → 立即拒发，"我的申请→详情"可见原因（如 `组合[本次申请] 仅 1 件，差额 4 件`），原因中无部件资产号。
2. applicant 从历史 10 件组中挑 2 件 → 拆出 2 件小组，拒发原因显示完整组合（含已撤下的 R-0003）。
3. applicant 选从未入选组的 5 件（部件 ID 31–35）→ 通过，公开页同事务出现新发布的样本数/失效数。
4. 用同一 `idempotency_key` 重试 → 返回同一申请号，不产生第二条发布。
5. maintainer 撤下该发布 → 公开页数字消失，但再提交相关申请时拒发原因仍把该发布计入历史组合。
6. 杀掉进程、把某申请置为 `processing` 后重启 → 日志显示恢复，申请回到队列重新裁决且不重复发布。

## API 一览

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` `/api/auth/logout` | 匿名 | 会话 Cookie（HttpOnly） |
| GET | `/api/auth/me` | 匿名 | 当前用户 |
| GET | `/api/public` | 匿名 | 在架发布的样本数/失效数 + 撤下编码公示 |
| GET | `/api/parts` | 机务 | 冻结批次部件名单（审计） |
| POST | `/api/applications` | 登录 | 提交申请，幂等键去重 |
| GET | `/api/applications` `/api/applications/{id}` | 登录 | 列表/详情（含拒发原因） |
| POST | `/api/releases/{id}/withdraw` | 机务或原申请单位 | 撤下，数据保留 |
| GET | `/api/audit` | 机务 | 审计轨迹 |

## 目录

```
benchguard/
├── benchguard/
│   ├── constants.py   # k=5、角色/状态枚举
│   ├── db.py          # SQLite 模式与 BEGIN IMMEDIATE 事务
│   ├── privacy.py     # 入选向量、分组、匿名判定（纯函数）
│   ├── repo.py        # 工作流：申请、裁决、撤下、查询、审计
│   ├── worker.py      # 串行裁决守护线程与中断恢复
│   ├── security.py    # PBKDF2 口令、会话令牌
│   ├── seed.py        # 幂等种子
│   ├── server.py      # HTTP 路由与静态资源
│   └── __main__.py    # 启动入口
├── web/               # 原生前端（index.html / app.js / styles.css）
└── tests/             # 23 项 unittest（纯逻辑/服务层/HTTP 并发）
```

## 测试

```bash
python3 -I -m unittest discover -s tests -v
```

覆盖：向量分组与全零组豁免、k=5 通过/拒发边界、拆组攻击、撤下仍核对、幂等重试、
中断恢复、批次外件拒收、输入校验、FIFO 串行裁决、角色越权（401/403）、6 线程并发提交各裁决一次。
