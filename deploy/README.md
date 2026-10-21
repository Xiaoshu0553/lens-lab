# 部署指南

本文使用保留示例域名 `study.example.com`。部署时将其替换为自己的域名；仓库不包含线上环境配置、密钥、数据库或研究记录。

## 本地 Docker 演示

需要 Docker Engine 或 Docker Desktop，以及 Compose v2。在项目根目录执行：

```bash
python3 scripts/setup.py
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

脚本为每次独立安装生成随机管理员密码和分组种子，默认 `STUDY_MODE=demo`。参与者入口为 <http://127.0.0.1:8000/>，管理员入口为 <http://127.0.0.1:8000/admin>，用户名 `admin`，密码在本地 `.env` 的 `ADMIN_PASSWORD` 中。演示无需模型密钥，从空记录开始，可体验问卷、规则对话、决策和导出流程。

`compose.yaml` 将应用端口绑定到本机回环地址，并将 `data/` 挂载到容器保存数据库。独立的 `synthetic-datasets/` 是可选只读历史合成包目录，缺失时归档下载不可用，不影响普通 demo。不要用 `compose.showcase.yaml` 代替首次安装：该文件是依赖外部批准来源包的隔离展示工具，不会自行生成展示数据。

## 使用真实模型

编辑本机 `.env`，将下面的示例值替换成自己的配置：

```dotenv
STUDY_DOMAIN=study.example.com
STUDY_MODE=pilot
PARTICIPANT_ACCESS_CODE=replace-with-a-random-invitation-code
MINOR_ACCESS_CODE=
UNDER14_PRIVACY_NOTICE_URL=
MODEL_BASE_URL=https://provider.example.com/v1
MODEL_API_KEY=replace-with-your-provider-key
MODEL_NAME=replace-with-your-model-id
MODEL_TEMPERATURE=0
MODEL_THINKING=disabled
MODEL_MAX_TOKENS=800
CLARIFICATION_STRATEGY=module
```

模型服务须提供兼容 OpenAI Chat Completions 的接口；适配逻辑见 `app/model.py`。本地服务可使用 `http://127.0.0.1:11434/v1` 等地址，且可不设密钥；若模型运行在宿主机、应用运行在 Docker 中，应改用容器可访问的宿主地址，`127.0.0.1` 在容器内指向容器本身。

管理员密码由 `scripts/setup.py` 生成，保存 `.env` 权限为 `600`。参与邀请码由研究者单独发放，避免公开服务被随意创建付费模型会话。切换 `live` 前必须填写邀请码、创建适用年龄段的正式任务，并完成问卷与评分定稿；预测试任务不会自动转成正式任务。模式及配置变动后重新创建容器。

未成年人正式参与需要研究者完成相应知情同意流程。`pilot/live` 下未配置 `MINOR_ACCESS_CODE` 时，12–17 岁入口关闭；12–14 岁组还需要 HTTPS 的 `UNDER14_PRIVACY_NOTICE_URL`。网页勾选与代码验证不能替代监护人同意记录。各模式及研究边界见 [研究设计](../docs/research-design.md)。

## HTTPS 与反向代理

若服务器 80/443 端口空闲，配置 `.env` 中的 `STUDY_DOMAIN` 后使用 Caddy：

```bash
docker compose --profile https config --quiet
docker compose --profile https up -d --build
```

域名 DNS 需要指向服务器，防火墙需允许 80 和 443 端口。若已有反向代理，仅运行普通 `docker compose up -d --build`，在现有代理中设置 HTTPS 站点并转发到 `http://127.0.0.1:8000`，避免重复占用端口。

`nginx-study-bootstrap.conf` 是首次申请证书的 HTTP 示例，`nginx-study.conf` 是签发证书后的 HTTPS 示例。将其中的域名与证书路径全部替换为自己的值，检查 Nginx 版本是否支持 `http2 on`，通过配置检查后再重载。续期钩子示例为 `certbot-reload.sh`，其中 Nginx 路径也需要按安装位置调整。

管理员网页登录有效期八小时，Cookie 具有 HttpOnly、SameSite=Strict 属性，在 HTTPS 下启用 Secure；修改管理员密码会使旧登录失效。脚本可使用 Basic 认证，所有管理接口都应通过 HTTPS 访问。

## 更新与一致性备份

更新代码前先停止应用，对数据库和配置做私有备份，再启动新版本：

```bash
docker compose stop app
```

将 `data/` 整个目录和 `.env` 复制到仓库外的私有备份位置；数据库以 SQLite WAL 模式运行，不要在写入期间只复制主数据库文件。随后：

```bash
git pull --ff-only
docker compose up -d --build
docker compose logs --tail 100 app
```

使用 Caddy 时，启动命令同样加上 `--profile https`。如需不停机备份，使用 SQLite backup API 等一致性备份方式。备份、日志、导出和真实对话都不应上传到公开 Issue 或仓库。

当前采用单进程与 SQLite，适用于本地展示和小规模预测试。扩大招募规模前，应在自己的目标环境完成并发、恢复及模型服务可用性验证。应用启动会初始化或迁移数据库，升级和回滚前都须核对数据兼容性。
