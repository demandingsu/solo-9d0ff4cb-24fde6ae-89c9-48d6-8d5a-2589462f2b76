# 野外水质探头 · 时钟回拨校时与质控隔离

单进程 FastAPI 应用：上传探头读数 CSV 与核时锚点 CSV，按设备时钟回拨分段、
用锚点线性校时并整版发布 UTC 日文件；质控员可按原始序号隔离清洗等场景下的
失真溶氧读数。

## 启动（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据（SQLite 数据库、原始 CSV、整版发布目录）保存在命名卷 `wq-data` 中，
容器重启不丢失；容器自带 `/healthz` 健康检查。

## 本地运行（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

环境变量：`WQ_DATA_DIR`、`WQ_DB_PATH`、`WQ_EXPORT_ROOT`（均有默认值）。

## 隔离规则

- 按**原始序号**隔离，必须填写原因；原始 CSV 与锚点记录保留不删。
- 隔离读数不进入 UTC 日文件，也不进入待核文件，只出现在同版的
  `excluded.csv` 隔离记录中。
- 隔离读数上的锚点停用、不参与校时；撤销隔离后锚点自动恢复有效。
- 回拨分段始终依据**完整原始序列**切分；排除读数不会合并分段，
  剩余读数只在各自分段内用有效锚点重算，不跨段插值。
- 每次隔离/撤销都在一个 SQLite 事务内整版重新发布；写文件或登记失败时
  整次回滚，旧版保留、旧链接仍下载原内容。

## 测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q
```
