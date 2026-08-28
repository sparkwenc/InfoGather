# InfoGather

本地 RSS 阅读与收藏工具。目前只实现了 arXiv RSS 的归一化，数据保存在 SQLite，支持 CLI 导出和本地 Web UI。

## 使用

```bash
cp conf/example.toml conf/config.toml  # 可选：创建自定义订阅配置
uv run inf ins
uv run infserve
```

默认访问地址是 <http://127.0.0.1:8787>。可以为 CLI 和服务端显式指定存储和配置：

```bash
uv run inf --db-path /path/entries.db ins --conf /path/config.toml
uv run infserve --db-path /path/entries.db --conf /path/config.toml
```

Web UI 中的“移除”只删除当前本地记录，不会屏蔽源；仍在 RSS 中的条目可能在下次拉取时重新出现。

## 验证

```bash
uv run python -m unittest discover -s test -p 'test_*.py'
uv build
```

25,000 条记录的独立性能预算检查（不包含在常规测试中）：

```bash
INFOGATHER_PERF_DIR=/path/to/temp uv run python -m unittest test.perf_storage
```
