# 安装、升级与回滚

本包 `v1.7.17-ops.20261020.1` 只支持官方v1.7.17 / `af706fc31e4a22cb2dea7442361b2b15185c3cee`。以下路径、Compose参数和服务名需要按自己的部署填写。Python3.11+，Linux/macOS示例；Windows使用同一安装器参数及完整路径。

## 核对输入

[Release下载](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.7.17-ops.20261020.1)含ZIP和SHA256SUMS.txt；ZIP顶层目录为`workbuddy2api-hub-ops-patch/`。

~~~sh
sha256sum -c SHA256SUMS.txt
git clone --depth 1 --branch v1.7.17 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.7.17
git -C workbuddy2api-hub-v1.7.17 rev-parse HEAD
PATCH_DIR='/absolute/path/to/workbuddy2api-hub-ops-patch'
TARGET_DIR='/absolute/path/to/workbuddy2api-hub-v1.7.17'
python3 -B "$PATCH_DIR/apply.py" --verify-package
python3 -B "$PATCH_DIR/apply.py" --target "$TARGET_DIR" --dry-run
~~~

SHA必须匹配上述完整提交；预检通过只代表可以应用，尚未部署。已有旧补丁或未知改动时，另建准确干净源码，保留旧部署。不删除状态/备份或重写校验绕过拒绝。回滚旧源码补丁使用其原版本安装器；当前安装器不能卸载旧包。

这也适用于旧补丁→v1.7.17：在新基线整体安装，不直接覆盖旧已安装树。运行时独立`usage/ops-observations.jsonl`不属于安装器修改范围；它有界轮换且可用`WB_OPS_OBSERVE=0`关闭，不改写原usage历史。

## 保存部署与数据

确认唯一目标服务、原Compose参数、环境配置、挂载、源码bind与启动方式。保存旧源码、所有Compose/env文件、原镜像及实际数据，保持权限；私有备份不上传GitHub。

~~~sh
COMPOSE_FILE='/absolute/path/to/your/compose.yaml'
SERVICE='your-hub-service'
BACKUP_DIR='/absolute/path/to/private-backups/pre-ops-20261020'
PATCHED_IMAGE='your-local/workbuddy2api-hub:1.7.17-ops.20261020.1'
ROLLBACK_IMAGE='your-local/workbuddy2api-hub:rollback-before-ops-20261020'
docker compose -f "$COMPOSE_FILE" config --services
CONTAINER_ID=$(docker compose -f "$COMPOSE_FILE" ps -q "$SERVICE")
docker inspect --format '{{json .Mounts}}' "$CONTAINER_ID"
install -d -m 700 "$BACKUP_DIR"
cp -p "$COMPOSE_FILE" "$BACKUP_DIR/compose.original.yaml"
~~~

暂停客户端请求、等待在途请求结束，只停止目标服务。源进程部署按原管理方式停止；不要让两个进程写同一数据。

~~~sh
docker compose -f "$COMPOSE_FILE" stop "$SERVICE"
docker commit "$CONTAINER_ID" "$ROLLBACK_IMAGE"
docker image save -o "$BACKUP_DIR/rollback-image.tar" "$ROLLBACK_IMAGE"
~~~

容器快照不包含挂载数据，也可能包含私有配置。停服后备份inspect确认的账号、settings、usage等目录，检查可读取；配置文件和原源码也要保留。准备失败时按原参数启动旧服务，不删除旧镜像或数据。

## 应用与构建

~~~sh
python3 -B "$PATCH_DIR/apply.py" --target "$TARGET_DIR" --dry-run
python3 -B "$PATCH_DIR/apply.py" --target "$TARGET_DIR"
docker build -t "$PATCHED_IMAGE" "$TARGET_DIR"
docker run --rm --network none --entrypoint python "$PATCHED_IMAGE" -B -c 'import wb_proxy; print(wb_proxy.retry_health_snapshot()["ops_patch_id"])'
~~~

安装替换proxy/accounts/webtools/upstream_pool四个Python模块及dashboard，新增ops/stall/observe三个模块。保留生成的安装状态和代码备份；源码回滚由同包处理。Docker镜像复制全部wb_*.py和dashboard，包括官方quota_scan/window_probe/update_request，不复制安装器/代码备份。导入检查应输出新ID，只证明代码存在，不是对话验收。

官方目录归档默认在`/app/catalog`。Docker应持久化此目录，或配置`WB_PROXY_CATALOG_DIR=/app/usage/catalog`复用现有usage挂载；不要覆盖现有usage。日志SQLite默认关闭，是否启用及保留上限由使用者决定。

原Compose仅将目标服务image改成固定新镜像，保留其余env、网络、Key、挂载和启动参数。旧源码bind会遮住镜像，应按自己的源码部署方式处理。原配置使用多个-f/project/profiles时，每条命令沿用它们。

~~~sh
docker compose -f "$COMPOSE_FILE" up -d --no-deps --no-build --pull never "$SERVICE"
~~~

无需整栈down，不更新依赖。源码部署沿原方式启动。

## 兼容与验收

v1.7.16提供可选`WB_CN_CHAT_UPSTREAM`，仅需要国内版改址时显式配置并重启；缺省沿用原址。它统一影响国内桌面/CLI对话、token刷新、模型目录和成长任务，账号X-Domain及国际版保持。示例为`https://www.workbuddy.cn`，公开安装器不设置此环境变量。

公共默认请求体仍16MiB。确有更大历史需求可显式设置`WB_MAX_PAYLOAD_BYTES=52428800`，评估并发内存。新checkpoint缓存可能重建，应保留原JSONL；退出身份、出口退避、账号权重/备注和国际活跃模型沿用原配置，不因更新自动开启新策略。补签/抽奖按原生规则使用卡券或次数。

v1.7.13～1.7.15新增额度窗口探针，原生`window_probe`缺省为true，会在重置时主动发起模型请求且不依赖签到开关。若保持旧版无主动探针行为，停服备份后仅添加`settings.json`的`"window_probe": false`，保留其它字段及权限。`quota_scan_models`默认空，主动额度扫描需自行选择模型。公开安装器不改这两项配置。

三协议适配器只支持整数n=1。更多候选在上游前返回400；单流守卫不是通用跨工具回合进展熔断。恢复提示现在默认开启，只用于未输出正文/拒答/工具的唯一一次循环恢复，可用`WB_OPS_LOOP_RECOVERY_HINT=0`关闭提示。响应头/首帧分别计时且共享原总期限，面板的较短超时仍有效。

通过原鉴权读取/health/retry，核对新ID、来源`wushuai0802/workbuddy2api-hub-ops-patch`及BASELINE.payload_fingerprint。原鉴权保持；未配置Key时没有额外防护。核对控制台、账号、配置、挂载、历史及真实正常流量的流式结束/工具结果/记账。健康、指纹和离线测试不能证明所有真实任务无循环。不要把账号、Token或完整日志贴到Issue。

## 回滚

源码用户停止进程，使用同包：

~~~sh
python3 -B "$PATCH_DIR/apply.py" --target "$TARGET_DIR" --rollback --dry-run
python3 -B "$PATCH_DIR/apply.py" --target "$TARGET_DIR" --rollback
~~~

恢复安装前的官方v1.7.17字节，不恢复更旧版本或业务数据；代码被再次修改时拒绝覆盖，需先检查。

Docker用户暂停请求，将image改回旧ROLLBACK_IMAGE，沿原参数定向启动。镜像缺失时从私有rollback-image.tar加载；当前账号/usage继续沿用，不自动用旧数据覆盖。恢复数据会丢失备份点之后新增内容，须另行保留当前数据并核对。

官方更新、直接换镜像或容器重建可能替换补丁；新基线需重新合并和验证，保留回滚路径。
## v1.7.6兼容选择

默认按模型名把terra/luna/5.3-codex改到DeepSeek，同名前台请求也匹配；默认禁用Messages及count_tokens。要保持旧模型路由，设置`WB_BACKGROUND_MODEL_NAMES=""`；要保留旧Messages接口，显式设置`WB_ENABLE_ANTHROPIC_MESSAGES=1`。后者仍不透传cache_control，有原有缓存/积分边界，不代表缓存问题已修好。按已有客户端需要选择，安装器不改部署配置或替你设置这两个变量。

v1.7.7/v1.7.8新增每日网页活跃重试/超时/次数与模型冷却回退、失败时间窗及一键测试。保留既有设置和账号/历史，新增字段沿原生默认按需读取。不要用真实账号的一键模型测试作为无成本健康检查；本地count_tokens仍是估算。国际版头对齐属于原生变更，离线检查不能保证所有上游会话错误消失。

## v1.7.10面板迁移

若原部署靠WB_ENABLE_ANTHROPIC_MESSAGES=1开启接口，升级后保持它会让面板关不掉：原生规则是settings为true或env为true。要交给面板控制，停服备份后将settings中的enable_anthropic_messages保持true，再清空该环境变量，确认鉴权和本地count_tokens；不要改写账号、Key、密码或其它设置。积分回退默认关闭，可显式free_model_credit_fallback=false且WB_ENABLE_FREE_MODEL_CREDIT_FALLBACK=0；0只是没有环境变量启用，不覆盖面板true。每Key并发0/空值保持原有不限行为。本公开安装器仍只改源码，运维者自行选择这些部署设置。
