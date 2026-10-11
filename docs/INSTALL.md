# 安装与回滚

本指南针对 **v1.6.20-ops.20261011.1**，只支持官方 v1.6.20 提交：

`cb6a844588705ea01388be2b9df1f0cbbbcde357`

以下命令为 Linux/macOS 的 shell 示例。所有绝对路径、Compose 服务名、镜像名和访问地址都由你替换为自己的值。Windows 源码安装可用 Python 3.11+ 执行相同的安装器参数，并使用自己的完整路径。

## 1. 获取并核对补丁与官方源码

可以使用 [Release ZIP](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.20-ops.20261011.1)，下载 `workbuddy2api-hub-v1.6.20-ops-20261011-r1.zip` 和 SHA256SUMS.txt 后，在两者所在目录执行：

~~~sh
sha256sum -c SHA256SUMS.txt
~~~

ZIP 顶层目录为 `workbuddy2api-hub-ops-patch/`；将 `PATCH_DIR` 设为解压后含 `apply.py` 的目录。也可以获取补丁仓库的固定 tag：

~~~sh
git clone --depth 1 --branch v1.6.20-ops.20261011.1 https://github.com/wushuai0802/workbuddy2api-hub-ops-patch.git ops-patch-v1620
git clone --depth 1 --branch v1.6.20 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.6.20
git -C workbuddy2api-hub-v1.6.20 rev-parse HEAD
~~~

核对输出的完整提交 SHA。工作目录内已经有同名文件时，先检查并换一个新目录，不覆盖旧部署。

~~~sh
PATCH_DIR='/absolute/path/to/ops-patch-v1620'
TARGET_DIR='/absolute/path/to/workbuddy2api-hub-v1.6.20'
python3 -B "$PATCH_DIR/apply.py" --verify-package
python3 -B "$PATCH_DIR/apply.py" --target "$TARGET_DIR" --dry-run
~~~

两个检查成功后再继续。`--verify-package` 只核对包；`--dry-run` 核对目标基线但不写文件。基线依赖也会检查，不能只凭版本字符串判断兼容。

如果目标已有旧补丁或本地改动，安装器可能拒绝。不要删除状态、修改校验清单或强行覆盖。保留旧源码、镜像和数据，在新目录准备准确的官方源码；本包不是旧补丁卸载器。

从 v1.6.19 补丁升级也使用新目录的官方 v1.6.20，不能在已打旧补丁的目录上叠加本包。仍使用 v1.6.19 时，请查看[历史 r2 Release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.19-ops.20261011.2)中的安装指南。

## 2. 保存自己的部署与数据

先确认当前管理方式、实际源码路径、账号/设置/用量目录、环境变量文件、数据挂载和启动参数。安装器只备份它修改的代码，不替你备份业务数据。

源码部署应保存当前源码、启动配置和数据，暂停客户端请求并等待在途请求结束，再停止网关进程。安装后沿用原启动方式，不同时启动两个进程写同一份数据。

Docker Compose 用户先设定自己的值：

~~~sh
COMPOSE_FILE='/absolute/path/to/your/compose.yml'
SERVICE='your-hub-compose-service'
BACKUP_DIR='/absolute/path/to/private-backups/pre-ops-20261011'
PATCHED_IMAGE='your-local-name/workbuddy2api-hub:1.6.20-ops.20261011.1'
ROLLBACK_IMAGE='your-local-name/workbuddy2api-hub:rollback-before-ops-20261011'

docker compose -f "$COMPOSE_FILE" config --services
docker compose -f "$COMPOSE_FILE" ps "$SERVICE"
CONTAINER_ID=$(docker compose -f "$COMPOSE_FILE" ps -q "$SERVICE")
docker inspect --format '{{json .Mounts}}' "$CONTAINER_ID"
docker inspect --format '{{.Image}}' "$CONTAINER_ID"
~~~

`SERVICE` 是 Compose 中的服务名，不是猜测出来的容器名。现有部署使用多个 `-f`、特定 project name、profiles 或其他参数时，每条命令都沿用这些参数。确认选择的是唯一目标服务。

在私有备份目录中保存所有 Compose 文件、环境配置与原源码：

~~~sh
install -d -m 700 "$BACKUP_DIR"
cp -p "$COMPOSE_FILE" "$BACKUP_DIR/compose.original.yml"
~~~

其他配置文件和数据位置以你的实际部署为准；不要把这些备份上传到公共仓库。暂停客户端流量，等在途请求结束后，只停止目标服务：

~~~sh
docker compose -f "$COMPOSE_FILE" stop "$SERVICE"
docker commit "$CONTAINER_ID" "$ROLLBACK_IMAGE"
docker image save -o "$BACKUP_DIR/rollback-image.tar" "$ROLLBACK_IMAGE"
~~~

快照用于保留可能写在旧容器层中的代码补丁；可能含私有配置，应本地保管。**容器快照不包含挂载数据**。停服后另行备份 inspect 确认的账号、设置与用量数据目录，保持权限和原路径，确认备份可读取后再继续。

若准备失败，可用原 Compose 文件重新启动旧服务。不要删除旧镜像、旧源码或数据备份。

## 3. 在准确的源码目录应用完整补丁

目标进程已停止，且已完成自己的备份后：

~~~sh
python3 -B "$PATCH_DIR/apply.py" --target "$TARGET_DIR" --dry-run
python3 -B "$PATCH_DIR/apply.py" --target "$TARGET_DIR"
~~~

应输出当前补丁 ID 和自动代码备份位置。安装记录保存在目标目录的 `.ops-patch-state.json`；保留它及对应代码备份，以便同一包回滚。

修改范围：

- 替换 `wb_proxy.py`、`wb_webtools.py` 和 `wb_accounts.py`。
- 新增 `wb_ops.py` 和 `wb_stall.py`。
- 不写账号、设置、用量、API Key、端口或其他业务配置。

整个包必须一起使用，不能只复制代理文件。源码运行的用户现在可以按原方式启动，并执行下面的部署验收。

## 4. Docker：从打过补丁的官方源码构建

~~~sh
docker build -t "$PATCHED_IMAGE" "$TARGET_DIR"
docker run --rm --network none --entrypoint python "$PATCHED_IMAGE" -B -c 'import wb_proxy; print(wb_proxy.retry_health_snapshot()["ops_patch_id"])'
~~~

第二条命令应输出 `v1.6.20-ops.20261011.1`。它使用空的容器数据和禁用的网络，只验证代码可导入和补丁标记，不能证明实际账号或对话可用。

官方 v1.6.20 Dockerfile 会复制 `wb_*.py`，因此包含这五个模块和上游新增的 `wb_upstream_pool.py` 连接池依赖。它不复制安装器、源码安装状态和代码备份，镜像部署应通过镜像切换回滚。

在原 Compose 文件中，仅将目标 Hub 服务的 `image` 改成刚构建的固定镜像名。保留其余环境变量、Key、网络、端口、挂载、启动参数和重启策略。检查是否有旧源码 bind mount 遮住新镜像；如有，先按自己的源码部署方式处理，不能忽略。

沿用原 Compose 参数，只重建该服务：

~~~sh
docker compose -f "$COMPOSE_FILE" up -d --no-deps --no-build --pull never "$SERVICE"
~~~

不需要整栈 `down`，也不要使用未指定服务的 `up` 来更新依赖。非 Compose 部署应使用你核对过的原参数创建新容器；本指南不替你推断 `docker run` 配置。

### v1.6.20 的配置兼容项

本补丁保留上游 **16 MiB** 的默认请求体上限。若你的客户端确实发送超过该值的 JSON 历史，可在目标服务环境变量中显式添加 `WB_MAX_PAYLOAD_BYTES: "52428800"`（50 MiB），并评估并发大请求的内存使用。它不是公共补丁的全局默认值；不要因安装补丁而自动调高。

保留原有调度配置，核对国内版连登管家的自动补签、兑换和抽奖行为；补签或抽奖会使用对应卡券/次数。「剩余用量优先调度」默认关闭，本补丁不代你开启。服务端 Docker/OpenWrt 看板的客户端「一键配置」不可用是上游 v1.6.20 的部署限制，并非安装失败。

上游连接复用默认开启，首帧与生成期限保护仍启用。单独排查复用行为时，可在自己的目标服务设置 `WB_UPSTREAM_KEEPALIVE=0` 并记录对照结果。

## 5. 部署后的验收

通过原有鉴权访问自己的 `/health/retry`，确认 `ops_patch_id` 为 `v1.6.20-ops.20261011.1`。该端点沿用现有 API-key 策略；未设置 Key 时没有额外鉴权。

检查启动日志、账号状态、原控制台、端口和数据挂载，确认配置与用量历史仍存在。不要把日志中的 Key、账号信息或完整配置贴到 Issue。

恢复自己的正常请求时，核对实际使用协议的流式/非流式响应、工具调用与用量记账。`/health` 的成功响应或补丁 ID 正确，只能证明基础运行状态。模型测试可能消耗你自己的额度，本包的离线验收没有替你执行这些请求。

如果刚更新后仍见偶尔的 “Let me go.”，不能据此认定补丁失效；查看是否持续高密度重复、错误码、工具结果是否变化，以及 `/health/retry` 中相应计数。一次正常 buffer release 不等于一次循环失败。

## 6. 回滚

### 源码安装：撤销本包

停止目标进程，用**同一补丁包**和原目标目录执行：

~~~sh
python3 -B "$PATCH_DIR/apply.py" --target "$TARGET_DIR" --rollback --dry-run
python3 -B "$PATCH_DIR/apply.py" --target "$TARGET_DIR" --rollback
~~~

回滚恢复此次保存的官方 v1.6.20 代码，保留代码备份。它不恢复更旧补丁、不同版本或账号数据。若安装后的代码又被修改，安装器会拒绝覆盖；先检查修改，不能强制回退。

### Docker 安装：切回旧镜像

暂停请求、停止目标服务，将其 Compose `image` 改回你保存的 `ROLLBACK_IMAGE`，用同样的定向 `up --no-deps --no-build --pull never` 启动。镜像不在本机时，从私有 `rollback-image.tar` 加载。

沿用当前数据挂载，不要自动把旧数据备份覆盖回去。若确需恢复数据，先保存回滚时的数据并核对差异；恢复旧备份会丢失对应恢复点之后的新增数据。回滚后再次检查原账号、设置、历史和真实工作流程。

## 后续官方更新

这个安装器不负责自动合并新上游版本。官方升级、重新拉取 `latest` 或改用官方镜像，都可能替换这些代码；本包的安装和回滚只认自己的准确基线和安装状态。新版本应另行准备、核对并验证，保留旧镜像与数据退路。
