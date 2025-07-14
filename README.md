# GitHub RSS Aggregator

🛰️ 一个基于 Flask 的 GitHub 发布信息聚合器，支持自动抓取多个仓库的 release 信息并以 RSS 2.0 格式输出。

## 特性
- 支持 GitHub Token（避免 API 限流）
- 支持动态配置、自动刷新、Web 编辑界面
- 并发获取仓库 Release
- RSS 2.0 输出，支持 RSS 阅读器订阅

## 界面
- 最大条目数为输出的RSS个数，并非监控库的数量，可以从主程序脚本里自行修改。MAX_ENTRIES = 100
![图片描述](https://i.postimg.cc/65vZ6FRW/1.png)
![图片描述](https://i.postimg.cc/t4GxsDth/2.png)

## rss输出地址
http://ip:5000/rss

## 主要文件
- github_rss.log----------------------------日志文件
- github_rss_aggregator.py---------------主程序
- repos.txt----------------------------------监控的库
- settings.json------------------------------动态设置配置
- token.txt-----------------------------------github token

## Docker安装
- 修改${GITHUB_RSS_DATA_DIR}为自定义挂载目录,安装后等待2分钟安装依赖包
```bash
docker run -d \
  --name github-rss-aggregator \
  --network bridge \
  --restart=always \
  -p 5000:5000 \
  -e TZ=Asia/Shanghai \
  -v ${GITHUB_RSS_DATA_DIR}:/app \
  -w /app \
  python:3.11 \
  bash -c "
    apt-get update && apt-get install -y git curl
    rm -rf /tmp/repo
    git clone https://github.com/NOwin111/GitHub-RSS-Aggregator.git /tmp/repo
    cp -r /tmp/repo/* /app/
    pip install flask feedparser requests
    python github_rss_aggregator.py
  "
