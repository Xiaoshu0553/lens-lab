# 部署指南

本文使用保留示例域名 `study.example.com`。部署时替换为自己的域名；仓库不包含线上配置、密钥、数据库或真实研究记录。当前方案 `university-v1` 仅面向 18–25 岁、当前在读的专科生和本科生。

## 本地 Docker 演示

需要 Docker Engine 或 Docker Desktop，以及 Compose v2。在项目根目录执行：

```bash
python3 scripts/setup.py
mkdir -p university-data university-showcase-data synthetic-datasets
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

脚本为独立安装生成随机管理员密码和分组种子，默认 `STUDY_MODE=demo`。参与者入口为 <http://127.0.0.1:8000/>，管理员入口为 <http://127.0.0.1:8000/admin>；用户名为 `admin`，密码在本地 `.env` 的 `ADMIN_PASSWORD` 中。普通演示无需模型密钥，从空记录开始。

本地 Python 默认数据库为 `./university-data/lens.sqlite3`。`compose.yaml` 将宿主 `university-data/` 挂载到 `/app/data`，并固定容器内 `DATABASE_PATH=/app/data/lens.sqlite3`。不要将 `.env` 的本地相对路径当成容器路径。已有 `.env` 不会被设置脚本覆盖，升级时应检查路径。

## 使用真实模型

编辑本机 `.env`，将示例值替换为自己的配置：

```dotenv
STUDY_DOMAIN=study.example.com
STUDY_MODE=pilot
DATABASE_PATH=./university-data/lens.sqlite3
PARTICIPANT_ACCESS_CODE=replace-with-a-random-invitation-code
MODEL_BASE_URL=https://provider.example.com/v1
MODEL_API_KEY=replace-with-your-provider-key
MODEL_NAME=replace-with-your-model-id
MODEL_TEMPERATURE=0
MODEL_THINKING=disabled
MODEL_MAX_TOKENS=1600
CLARIFICATION_STRATEGY=prompt
```

两组使用同一模型、共同基础提示、公共资料、参数及完整对话历史。澄清组在对话前额外收到主动澄清提示，并在后续请求中保留；标准组没有这项额外行为要求，仍可能自然追问。两组均须回答用户当前问题，信息充分时直接回答。旧 `module` 策略配置仅兼容映射为 `prompt`。

模型服务须提供 `{MODEL_BASE_URL}/chat/completions` 接口。本地服务可使用 `http://127.0.0.1:11434/v1` 等地址且可不设密钥；若模型在宿主机而应用在 Docker 中，须使用容器可访问的宿主地址。容器内 `127.0.0.1` 指向容器本身。

服务报告输出达到 `MODEL_MAX_TOKENS` 上限而截断时，应用返回本轮可重试错误，不保存半截回复，也不计入有效对话轮次。修改模型、提示词或参数后重新创建容器，并开始新会话。两组澄清计数均依据相同输出标记，漏标会漏计，应配合完整对话审核。

管理员密码由设置脚本生成，`.env` 权限为 `600`。参与邀请码由研究者单独发放。切换 `live` 前应填写邀请码、创建当前大学生版本的正式任务，并完成材料与评分定稿；预测试任务不会自动转为正式任务。当前各模式均要求大学生年龄、在读学段和明确的在读确认，不使用旧跨年龄准入配置。

## 当前合成展示

展示实例使用 `SHOWCASE_MODE=1`、`demo` 和独立 `showcase.sqlite3`。启动时从当前大学生材料及确定性生成器创建 126 条已完成合成记录，不需要旧 600 条跨年龄来源包：

```bash
docker compose -f compose.showcase.yaml config --quiet
docker compose -f compose.showcase.yaml up -d --build showcase
docker compose -f compose.showcase.yaml ps showcase
```

宿主 `university-showcase-data/` 挂载到容器 `/app/showcase-data`，端口为 `127.0.0.1:8001`。数据库必须使用专用名称 `showcase.sqlite3`，不得指向真实研究库。合成下载目录只列当前 `university-v1`、批次 `student-126-20261022-v1`；旧 600 条数据及旧生成器属于历史归档。

126 条记录、模拟对话、分数和时点用于展示导出与统计流程，不是受试者作答，不是实际模型输出，也不能证明大学生样本中的组间效应。普通 `demo` 不自动填充这些记录。

## HTTPS 与反向代理

若服务器 80/443 端口空闲，配置 `.env` 中的 `STUDY_DOMAIN` 后可使用 Caddy：

```bash
docker compose --profile https config --quiet
docker compose --profile https up -d --build
```

域名 DNS 需指向服务器，防火墙需允许 80 和 443。已有反向代理时，仅运行普通 Compose，在现有代理中配置 HTTPS 并转发到所需端口，避免重复占用端口。

`nginx-study-bootstrap.conf` 是首次申请证书的 HTTP 示例，`nginx-study.conf` 是签发证书后的 HTTPS 示例。替换全部域名与证书路径，检查 Nginx 是否支持 `http2 on`，通过配置检查后再重载。续期钩子为 `certbot-reload.sh`，其中 Nginx 路径也需按安装位置调整。

管理员网页登录有效期八小时，Cookie 使用 HttpOnly、SameSite=Strict，HTTPS 下启用 Secure；修改管理员密码会使旧登录失效。脚本可使用 Basic 认证，管理接口应通过 HTTPS 访问。

## 同域展示与真实模型试跑

同域部署可将根站及后台图表转发到 `127.0.0.1:8001` 的大学生合成展示实例；`/admin/preview` 转发到 `127.0.0.1:8000` 的真实模型 `pilot` 实例，经 `/preview-api/` 访问独立 API。两个实例都升级为当前大学生材料，数据保持隔离：

| 用途 | 宿主路径（安装目录为 `/opt/lens_lab`） | 容器路径 |
| --- | --- | --- |
| 真实模型试跑 | `/opt/lens_lab/university-data/lens.sqlite3` | `/app/data/lens.sqlite3` |
| 当前 126 条合成展示 | `/opt/lens_lab/university-showcase-data/showcase.sqlite3` | `/app/university-showcase-data/showcase.sqlite3` |
| 旧研究库／旧展示库 | 原 `data/`、`showcase-data/` | 仅保留历史，不挂载为当前库 |

先停止旧实例并一致性备份数据库和配置，保留原目录，再建立上述新目录。两个实例须使用相同的 `ADMIN_PASSWORD` 和兼容的管理员 Cookie 格式，以便沿用同域登录。分别检查并更新两个实例：

```bash
docker compose -f compose.yaml -f compose.preview.yaml config --quiet
docker compose -f compose.yaml -f compose.preview.yaml up -d --build app
docker compose -f compose.showcase.yaml config --quiet
docker compose -f compose.showcase.yaml up -d --build showcase
```

`compose.preview.yaml` 设置 `PREVIEW_API_PREFIX=/preview-api`、`RESEARCHER_PREVIEW_ONLY=1` 和输出上限 1600。此实例 API 严格要求管理员认证，只接受 `researcher_preview=true` 的新会话，拒绝普通参与者记录；无需登录的 `/healthz` 仅用于健康检查。研究者试跑仍须确认年龄及在读口径，记录不进入普通参与者分析，也不修改合成展示库。

将 [nginx-preview-routes.conf](nginx-preview-routes.conf) 包含到现有 HTTPS `server` 块，例如：

```nginx
include /opt/lens_lab/deploy/nginx-preview-routes.conf;
```

片段增加三个路由：`/admin/preview` 和 `/static/app.js` 转到真实试跑实例；`/preview-api/` 转发时剥离前缀。`location /` 继续转发到 8001，但其上游应为新版大学生展示实例。保留 Host、来源地址和 HTTPS 协议，不为此 API 前缀开放跨域；已有同名路由时先合并，检查配置通过后重载。

验证时登录后台打开 `/admin/preview`，核对三年龄区间和专科／本科、共同 30 题、在读确认，以及 `/preview-api/api/...` 请求。新会话应标记 `researcher_preview` 并使用真实模型；退出登录后 API 应拒绝访问。根站后台应显示当前 126 条大学生合成记录及其来源说明，不显示旧 600 条为当前数据。前端存储使用新的 `lens_university_v1_*` 命名空间，不续接旧跨年龄会话。

## 更新与一致性备份

更新前停止相应实例，对其数据目录和 `.env` 做私有备份。SQLite 使用 WAL，不要在写入期间只复制主数据库文件；可停服务后复制整个目录，或使用 SQLite backup API。

```bash
docker compose stop app
```

备份 `university-data/` 后更新并重新启动；展示实例同理备份 `university-showcase-data/`，使用其独立 Compose 文件：

```bash
git pull --ff-only
docker compose up -d --build
docker compose logs --tail 100 app
```

使用 Caddy 时加 `--profile https`；管理员专用试跑部署则保留 `compose.preview.yaml` 覆盖参数。不要把旧目录复制进当前大学生数据库，也不要按新材料重评分历史记录。备份、日志、导出和真实对话不应上传到公开仓库或 Issue。

当前单进程 SQLite 适用于本地展示和小规模预测试。扩大招募前应在目标环境验证并发、恢复与模型服务可用性。启动会初始化或迁移数据库，升级和回滚前应核对版本及数据兼容性。
