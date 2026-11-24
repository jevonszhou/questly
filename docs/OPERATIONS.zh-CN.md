# Questly 部署与数据运维

## 首次部署

Questly 使用 MongoDB 7 单成员副本集，以支持跨集合事务。Compose 会启动 MongoDB，并通过 `mongo-init` 初始化 `rs0`；该副本集只提供事务能力，不提供高可用。数据库端口不映射到宿主机。

首次运行前复制 `.env.example` 为 `.env`，将 `SECRET_KEY` 改为随机长密钥，将 `TZ` 设为家庭所在时区。若在 HTTPS 反向代理后运行，将 `SESSION_COOKIE_SECURE=true`。然后执行：

```sh
docker compose up --build -d
docker compose exec web flask --app wsgi:app migrate-v2
```

首次访问 `http://localhost:37000`，按页面提示创建家长账户、家庭名称和时区。为每名孩子设置 4 至 6 位 PIN。不要把服务直接暴露到公网；远程访问使用可信 HTTPS 反向代理及 VPN。

## 备份

备份文件应写入与 MongoDB 数据卷不同的存储故障域，并限制访问权限：

```sh
tools/backup-mongo.sh /path/to/independent-backups
```

脚本生成 gzip 压缩的 `mongodump` 归档。按家庭设备容量保留近期每日、每周和每月副本，并将至少一份备份复制到独立设备或 NAS/PBS。

## 恢复演练

先恢复到独立数据库名称，检查账户、积分流水和宠物事件数量及关系：

```sh
tools/restore-check-mongo.sh /path/to/questly-backup.archive.gz
```

演练确认后，如需恢复正式数据，先停止 Web 写入并再次备份当前数据库，再由管理员按维护窗口执行恢复。不要使用 `docker compose down -v`；该命令会删除 Compose 命名数据卷。

## 迁移

在新旧版本切换前暂停写入并备份。`migrate-v2` 可重复执行：保留已有流水，补齐用户语言和积分累计字段，为尚无活动宠物的孩子创建初始档案。旧流水不回溯生成 HP 事件。请在隔离数据库完成恢复及迁移演练后再更新正式实例。
