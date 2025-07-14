# GitHub RSS Aggregator

🛰️ 一个基于 Flask 的 GitHub 发布信息聚合器，支持自动抓取多个仓库的 release 信息并以 RSS 2.0 格式输出。

## 特性
- 支持 GitHub Token（避免 API 限流）
- 支持动态配置、自动刷新、Web 编辑界面
- 并发获取仓库 Release
- RSS 2.0 输出，支持 RSS 阅读器订阅

## 安装与运行

```bash
docker run -d \
  -p 3500:5000 \
  -v $PWD:/app \
  -w /app \
  python:3.11 \
  bash -c "pip install flask feedparser requests && python github_rss_aggregator.py"
