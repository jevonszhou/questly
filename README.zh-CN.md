# Questly 家庭积分与宠物养成

Questly 是面向单个家庭的自托管应用。家长创建任务、审批申请并管理奖励；孩子在手机浏览器或 PWA 中完成任务、查看积分和照顾宠物。当前部署包含 Flask、MongoDB 7 和 Docker Compose。

## 本地启动

需要安装 Docker Engine 和 Docker Compose 插件。Windows 用户可使用 Docker Desktop。

1. 将 `.env.example` 复制为 `.env`，设置随机 `SECRET_KEY`；默认时区为 `Asia/Shanghai`。
2. 启动应用：

   ```sh
   docker compose up --build -d
   ```

3. 打开 `http://localhost:37000`，创建首位家长账户、家庭名称和时区。
4. 在“家庭成员”页面添加孩子，并为每个孩子设置 4 至 6 位 PIN。

MongoDB 以单成员副本集 `rs0` 启动，为跨集合事务提供支持。它不提供高可用。数据库容器不向宿主机发布 27017 端口。只在可信家庭网络或 HTTPS 反向代理后使用此服务。

## 现有数据库升级

升级前暂停写入并完成备份。部署新版本后执行幂等迁移：

```sh
docker compose exec web flask --app wsgi:app migrate-v2
```

迁移会为孩子补齐语言、状态、累计积分字段，为尚无活动宠物的孩子创建初始宠物；旧积分流水保持不变，不追溯创建宠物事件。旧账户若没有 PIN，需要家长在“家庭成员”中设置 PIN 后才能登录。

## 数据备份与恢复演练

请把备份保存到与 MongoDB 数据卷不同的设备或故障域：

```sh
tools/backup-mongo.sh /path/to/independent-backups
tools/restore-check-mongo.sh /path/to/questly-backup.archive.gz
```

第二条命令恢复到单独的 `questly_restore_check` 数据库，不会删除或覆盖线上库。部署与恢复步骤见 [运维手册](docs/OPERATIONS.zh-CN.md)。

## 重要配置

- `SECRET_KEY`：必须使用随机密钥，保存在 `.env`，不要提交版本控制。
- `TZ`：家庭时区，默认 `Asia/Shanghai`。
- `PORT`：Web 端口，默认 `37000`。
- `SESSION_COOKIE_SECURE=true`：仅在 HTTPS 反向代理配置完成后启用。

儿童积分、任务和宠物 HP 数据保存在 MongoDB 命名卷中。不要执行 `docker compose down -v`，该命令会删除数据卷。远程访问请使用 VPN 或可信 HTTPS 反向代理，不要直接暴露到公网。
