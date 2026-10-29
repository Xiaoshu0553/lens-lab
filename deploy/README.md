# 部署指南

当前默认部署为翻译信息公平性研究平台，项目为《从“直接翻译”到“主动澄清”：面向翻译使用者的信息公平性研究与促进——基于生成式人工智能轻量化优化模块的设计与实证》——以中国矿业大学翻译用户为例。

默认参与者和研究后台为 `/` 与 `/admin`；`/admin/preview` 用于独立试跑，`/admin/instruments` 用于翻译材料审查。新版使用 `/api/translation`、`/api/admin/translation` 以及独立的翻译记录表，旧校园任务与合成记录不进入翻译统计。`demo` 不生成研究评分；`pilot` 使用真实模型；`live` 要求邀请码，并在后台确认冻结当前问卷、任务、量规、提示与模型配置。完整流程见 [翻译研究说明](../docs/translation-study.md)。

`compose.yaml` 现在将宿主 `translation-data/` 挂载到 `/app/data`。升级时先备份旧 `university-data/`；不要把旧数据库内容当作翻译参与者数据导入。已有 `.env` 保持原样，本地 Python 使用时需手动检查 `DATABASE_PATH=./translation-data/lens.sqlite3`。新安装运行 `python3 scripts/setup.py` 会生成该默认路径。

以下原有部署章节保留历史平台的部署方法和展示站点维护信息；其中 `university-v1` 的两组、30题、决策评分与126条合成展示均属于历史实验，当前翻译研究使用 S／A 两种条件和翻译材料。普通 Docker 启动命令及路径已更新，新翻译实验无需启动历史 showcase 服务。

本文使用保留示例域名 `study.example.com`。部署时替换为自己的域名；仓库不包含线上配置、密钥、数据库或真实研究记录。当前翻译候选方案面向中国矿业大学年满18岁的在读专科、本科及研究生翻译使用者，页面自报确认不替代实际招募核验。

## 本地 Docker 演示

需要 Docker Engine 或 Docker Desktop，以及 Compose v2。在项目根目录执行：

```bash
python3 scripts/setup.py
mkdir -p translation-data
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

脚本为独立安装生成随机管理员密码和分组种子，默认 `STUDY_MODE=demo`。参与者入口为 <http://127.0.0.1:8000/>，管理员入口为 <http://127.0.0.1:8000/admin>；用户名为 `admin`，密码在本地 `.env` 的 `ADMIN_PASSWORD` 中。普通演示无需模型密钥，从空记录开始。

本地 Python 默认数据库为 `./translation-data/lens.sqlite3`。`compose.yaml` 将宿主 `translation-data/` 挂载到 `/app/data`，固定容器内 `DATABASE_PATH=/app/data/lens.sqlite3` 与 `TRANSLATION_MODELS_PATH=/app/data/.translation-models.json`。后台保存的云端密钥和模型配置随该目录持久化，容器重建不会丢失。不要将 `.env` 的本地相对路径当成容器路径。已有 `.env` 不会被设置脚本覆盖，升级时应检查路径。

真实模型预测试使用 `pilot`。后台“模型与 API”可分别配置翻译对话模型和5–9个不同评审模型，各评审模型按同一套五维量规完整评分；去掉一个最高和一个最低模型总分后，按剩余模型预设权重归一化加权。保存配置与真实连通检查分别记录，模型地址、密钥存在不表示调用已成功；没有完整评审团时不生成综合分。`demo` 仍仅作规则演示，不运行研究评分。

## 使用真实模型

编辑本机 `.env`，将示例值替换为自己的配置：

```dotenv
STUDY_DOMAIN=study.example.com
STUDY_MODE=pilot
DATABASE_PATH=./translation-data/lens.sqlite3
PARTICIPANT_ACCESS_CODE=replace-with-a-random-invitation-code
MODEL_BASE_URL=https://provider.example.com/v1
MODEL_API_KEY=自行填写API
MODEL_NAME=replace-with-your-model-id
MODEL_TEMPERATURE=0
MODEL_THINKING=disabled
MODEL_MAX_TOKENS=1600
CLARIFICATION_STRATEGY=prompt
```

翻译研究当前只开放 S 直接翻译、A 主动澄清两种条件，使用同一基础模型与共同原文。须先完成公平性基线测试，再根据结果设计后续优化模块；当前不启用优化组。对话接收完整历史，模型不自动获得参与者补充资料或参考译文。`CLARIFICATION_STRATEGY` 是历史校园任务的兼容选项，不选择翻译条件。

模型服务须提供 `{MODEL_BASE_URL}/chat/completions` 接口。本地服务可使用 `http://127.0.0.1:11434/v1` 等地址且可不设密钥；若模型在宿主机而应用在 Docker 中，须使用容器可访问的宿主地址。容器内 `127.0.0.1` 指向容器本身。

服务报告输出达到 `MODEL_MAX_TOKENS` 上限而截断时，应用返回本轮可重试错误，不保存半截回复，也不计入有效对话轮次。修改模型、提示词或参数后重新创建容器，并开始新会话。翻译回复通过统一 JSON 协议记录回答或澄清类型，类型由模型判断，应配合完整对话审查。

管理员密码由设置脚本生成，`.env` 权限为 `600`。参与邀请码由研究者单独发放。切换 `live` 前应填写邀请码、审查当前翻译问卷、任务、量规与模型配置，并在研究后台确认冻结；更换材料、模型配置或实验时间与轮数限制后需重新冻结。预测试和正式记录的配置指纹不同，不自动合并。

## 历史校园合成展示（不用于翻译研究）

展示实例使用 `SHOWCASE_MODE=1`、`demo` 和独立 `showcase.sqlite3`。启动时从当前大学生材料及确定性生成器创建 126 条已完成合成记录，不需要旧 600 条跨年龄来源包：

```bash
docker compose -f compose.showcase.yaml config --quiet
docker compose -f compose.showcase.yaml up -d --build showcase
docker compose -f compose.showcase.yaml ps showcase
```

宿主 `university-showcase-data/` 挂载到容器 `/app/university-showcase-data`，端口为 `127.0.0.1:8001`。数据库必须使用专用名称 `showcase.sqlite3`，不得指向真实研究库。合成下载目录只列当前 `university-v1`、批次 `student-126-20261022-v4`；旧 600 条数据及旧生成器属于历史归档。

V4沿用V3的126条个人数据、对话正文、动作、时点及线索计数，仅新增逐轮回复耗时估计。逐条A组2–8次、S组1–5次来源，范围重叠；每卡最多两次且不连续，年龄区间使用不同兴趣权重。初始事实可为空、仅既往名额、仅每周到场或两项均有；A只询问未给事实0–2次，S不主动问。每次 `evidence_reveal` 对应实际模板回复；过程规则是人工展示假设，不证明真实模型或用户效应，也不调节个人分数。AI平均用时读取按回复长度、动作类型及固定seed估算的逐轮值，估计事件为 `reply_time_estimate`，记录JSON和归档保留 `reply_time_source=estimated` 与估计器版本；实际模型延迟和前测耗时仍为空，不生成 `reply_latency` 实测事件，不把估计作性能评测。真实记录继续使用实测日志，缺失不补；过程提醒从答案及对话派生，不自动排除记录。普通 `demo` 不自动填充这些记录。

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
| 旧研究库／旧展示库／旧来源包 | 服务器外私有备份 | 不挂载到当前实例 |

先停止旧实例，对数据库与配置做一致性备份并转存到服务器外的私有位置，核验可恢复后移除服务器旧 `data/`、`showcase-data/`、旧 600 条来源包、V1、V2、V3 展示库及临时库；不得先删除后补备份。旧批次收据不能用于V4，须建立全新的V4展示库；当前服务器仅保留 `university-data/` 和 `university-showcase-data/` 两个运行数据目录。两个 Compose 服务均不再挂载旧 `synthetic-datasets/`。两个实例须使用相同的 `ADMIN_PASSWORD` 和兼容的管理员 Cookie 格式，以便沿用同域登录。分别检查并更新两个实例：

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

验证时登录后台打开 `/admin/preview`，核对三年龄区间和专科／本科、共同 30 题、在读确认，以及 `/preview-api/api/...` 请求。新会话应标记 `researcher_preview` 并使用真实模型；退出登录后 API 应拒绝访问。根站后台和五项常用导出应连接同一个 V4 数据库，记录编号、分数、对话及线索计数一致；三类常用CSV仅含业务列，常用JSON与完整归档机器元数据保留来源和资格，不将旧 600 条或 V1 展示为当前数据。前端存储使用新的 `lens_university_v1_*` 命名空间，不续接旧跨年龄会话。

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
