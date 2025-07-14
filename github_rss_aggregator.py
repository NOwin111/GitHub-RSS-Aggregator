from flask import Flask, Response, jsonify, request, render_template_string
import requests
import feedparser
from xml.etree.ElementTree import Element, SubElement, tostring, indent
from datetime import datetime, timezone
import html
import os
import logging
from logging.handlers import RotatingFileHandler
import concurrent.futures
from threading import Lock, Thread
import time
from urllib.parse import quote
import re
from email.utils import formatdate
import json

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            'github_rss.log', 
            maxBytes=200*1024,
            backupCount=1,
            encoding='utf-8'
        )
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 配置常量
REPOS_FILE = "repos.txt"
TOKEN_FILE = "token.txt"
SETTINGS_FILE = "settings.json"
MAX_ENTRIES = 100
REQUEST_TIMEOUT = 10
MAX_WORKERS = 20

# 默认配置 - 现在这些会从文件加载
DEFAULT_CACHE_DURATION = 300  # 5分钟缓存
DEFAULT_AUTO_REFRESH_INTERVAL = 1800  # 30分钟自动刷新
DEFAULT_STARTUP_REFRESH = True  # 默认启用启动刷新

# 启动刷新环境变量控制
STARTUP_REFRESH = os.getenv("STARTUP_REFRESH", "true").lower() == "true"

# 动态配置变量
settings = {
    'cache_duration': DEFAULT_CACHE_DURATION,
    'auto_refresh_interval': DEFAULT_AUTO_REFRESH_INTERVAL,
    'startup_refresh': DEFAULT_STARTUP_REFRESH
}

# 全局缓存
cache = {
    'data': None,
    'timestamp': 0,
    'lock': Lock()
}

# 自动刷新标志
auto_refresh_running = False
refresh_thread = None

def load_settings():
    """加载系统设置"""
    global settings
    default_settings = {
        'cache_duration': DEFAULT_CACHE_DURATION,
        'auto_refresh_interval': DEFAULT_AUTO_REFRESH_INTERVAL,
        'startup_refresh': DEFAULT_STARTUP_REFRESH
    }
    
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded_settings = json.load(f)
                default_settings.update(loaded_settings)
                settings.update(default_settings)
                logger.info(f"系统设置已加载: 缓存持续时间={settings['cache_duration']}秒, "
                           f"自动刷新间隔={settings['auto_refresh_interval']}秒, "
                           f"启动时刷新={settings['startup_refresh']}")
        except Exception as e:
            logger.error(f"加载设置文件时出错: {e}")
            settings.update(default_settings)
    else:
        logger.info("设置文件不存在，使用默认配置")
        settings.update(default_settings)

def save_settings():
    """保存系统设置"""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        logger.info(f"系统设置已保存: 缓存持续时间={settings['cache_duration']}秒, "
                   f"自动刷新间隔={settings['auto_refresh_interval']}秒, "
                   f"启动时刷新={settings['startup_refresh']}")
        return True
    except Exception as e:
        logger.error(f"保存设置时出错: {e}")
        return False

def startup_cache_warmup():
    """启动时预热缓存"""
    # 检查是否启用启动刷新
    startup_refresh_enabled = STARTUP_REFRESH and settings.get('startup_refresh', True)
    
    if not startup_refresh_enabled:
        logger.info("启动时刷新已禁用，跳过缓存预热")
        return
    
    logger.info("容器启动，开始预热缓存...")
    start_time = time.time()
    
    try:
        # 预加载数据到缓存
        entries = fetch_all_releases()
        rss_xml = create_rss_feed(entries)
        
        with cache['lock']:
            cache['data'] = rss_xml
            cache['timestamp'] = time.time()
        
        elapsed_time = time.time() - start_time
        logger.info(f"缓存预热完成！获取到 {len(entries)} 个发布条目，耗时 {elapsed_time:.2f} 秒")
            
    except Exception as e:
        logger.error(f"启动时缓存预热失败: {e}")

def restart_auto_refresh():
    """重启自动刷新服务"""
    global auto_refresh_running, refresh_thread
    
    # 停止现有的自动刷新
    if auto_refresh_running:
        auto_refresh_running = False
        if refresh_thread and refresh_thread.is_alive():
            logger.info("正在停止现有的自动刷新服务...")
            # 等待一小段时间让线程自然结束
            time.sleep(1)
    
    # 启动新的自动刷新
    refresh_thread = start_auto_refresh()
    logger.info(f"自动刷新服务已重启，新的刷新间隔: {settings['auto_refresh_interval']}秒")

def auto_refresh_worker():
    """自动刷新后台任务"""
    global auto_refresh_running
    auto_refresh_running = True
    logger.info(f"自动刷新任务启动，每 {settings['auto_refresh_interval'] // 60} 分钟刷新一次")
    
    while auto_refresh_running:
        time.sleep(settings['auto_refresh_interval'])
        if auto_refresh_running:  # 再次检查，防止程序退出时仍在执行
            logger.info("执行自动刷新...")
            try:
                # 清空缓存，强制下次请求重新获取数据
                with cache['lock']:
                    cache['data'] = None
                    cache['timestamp'] = 0
                
                # 预加载数据到缓存
                entries = fetch_all_releases()
                rss_xml = create_rss_feed(entries)
                
                with cache['lock']:
                    cache['data'] = rss_xml
                    cache['timestamp'] = time.time()
                
                logger.info("自动刷新完成")
            except Exception as e:
                logger.error(f"自动刷新时出错: {e}")

def start_auto_refresh():
    """启动自动刷新后台线程"""
    refresh_thread = Thread(target=auto_refresh_worker, daemon=True)
    refresh_thread.start()
    return refresh_thread

def load_github_token():
    """加载GitHub Token"""
    # 首先尝试从文件读取
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                token = f.read().strip()
                if token:
                    logger.info("从token.txt文件加载GitHub Token")
                    return token
        except Exception as e:
            logger.error(f"读取token文件时出错: {e}")
    
    # 如果文件不存在或为空，尝试从环境变量读取
    token = os.getenv("GITHUB_TOKEN", "")
    if token:
        logger.info("从环境变量加载GitHub Token")
    else:
        logger.warning("未找到GitHub Token")
    
    return token

def save_github_token(token):
    """保存GitHub Token到文件"""
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(token.strip())
        logger.info("GitHub Token已保存到token.txt")
        return True
    except Exception as e:
        logger.error(f"保存token时出错: {e}")
        return False

def load_repos():
    """加载仓库列表"""
    if not os.path.exists(REPOS_FILE):
        logger.warning(f"仓库文件 {REPOS_FILE} 不存在")
        return []
    
    repos = []
    try:
        with open(REPOS_FILE, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                
                if is_valid_repo_format(line):
                    repos.append(line)
                else:
                    logger.warning(f"第 {line_num} 行格式无效: {line}")
        
        logger.info(f"成功加载 {len(repos)} 个仓库")
        return repos
    except Exception as e:
        logger.error(f"读取仓库文件时出错: {e}")
        return []

def save_repos(repos_text):
    """保存仓库列表到文件"""
    try:
        with open(REPOS_FILE, "w", encoding="utf-8") as f:
            f.write(repos_text)
        logger.info("仓库列表已保存到repos.txt")
        return True
    except Exception as e:
        logger.error(f"保存仓库列表时出错: {e}")
        return False

def is_valid_repo_format(repo):
    """验证仓库名格式"""
    pattern = r'^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$'
    return bool(re.match(pattern, repo))

def format_rfc822_date(iso_date_string):
    """将ISO格式日期转换为RFC 822格式（RSS 2.0标准）"""
    try:
        # 解析ISO格式的日期字符串
        if iso_date_string:
            # 处理不同的日期格式
            if 'T' in iso_date_string:
                if iso_date_string.endswith('Z'):
                    dt = datetime.fromisoformat(iso_date_string.replace('Z', '+00:00'))
                elif '+' in iso_date_string or iso_date_string.endswith('00:00'):
                    dt = datetime.fromisoformat(iso_date_string)
                else:
                    dt = datetime.fromisoformat(iso_date_string + '+00:00')
            else:
                # 如果是简单的日期格式，尝试解析
                dt = datetime.fromisoformat(iso_date_string)
            
            # 转换为UTC时间戳，然后格式化为RFC 822
            timestamp = dt.timestamp()
            return formatdate(timestamp, usegmt=True)
        else:
            # 如果没有日期，使用当前时间
            return formatdate(time.time(), usegmt=True)
    except Exception as e:
        logger.warning(f"日期格式转换失败: {e}, 使用当前时间")
        return formatdate(time.time(), usegmt=True)

def fetch_repo_releases(repo):
    """获取单个仓库的发布信息"""
    url = f"https://github.com/{repo}/releases.atom"
    entries = []
    
    try:
        logger.debug(f"正在获取 {repo} 的发布信息...")
        
        # 设置请求头
        headers = {
            'User-Agent': 'GitHub-RSS-Aggregator/1.0',
            'Accept': 'application/atom+xml, application/xml, text/xml'
        }
        
        # 获取当前的token
        github_token = load_github_token()
        if github_token:
            headers["Authorization"] = f"token {github_token}"
            logger.debug(f"使用了 GitHub Token（前8位）: {github_token[:8]}********")
        
        # 先检查URL是否可访问
        response = requests.head(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            logger.warning(f"仓库 {repo} 没有发布页面或不存在")
            return entries
        elif response.status_code != 200:
            logger.warning(f"仓库 {repo} 返回状态码: {response.status_code}")
            return entries
        
        # 解析RSS Feed
        feed = feedparser.parse(url)
        
        if feed.bozo:
            logger.warning(f"仓库 {repo} RSS格式可能有问题: {feed.bozo_exception}")
        
        if not hasattr(feed, 'entries') or not feed.entries:
            logger.warning(f"仓库 {repo} 没有发布条目")
            return entries
        
        # 只取第一个（最新的）发布条目
        if feed.entries:
            entry = feed.entries[0]  # 取最新的一条
            
            # 处理发布时间
            updated_time = entry.get('updated', entry.get('published', ''))
            if not updated_time:
                updated_time = datetime.now(timezone.utc).isoformat()
            
            # 处理作者信息
            author_name = ""
            if hasattr(entry, 'author_detail') and entry.author_detail:
                author_name = entry.author_detail.get('name', '')
            elif hasattr(entry, 'author'):
                author_name = entry.author
            
            # 处理摘要
            summary = entry.get('summary', entry.get('description', ''))
            if summary:
                # 清理HTML标签
                summary = html.unescape(summary)
                # 限制摘要长度
                if len(summary) > 500:
                    summary = summary[:500] + '...'
            
            # 提取仓库名称（去掉用户名部分）
            repo_name = repo.split('/')[-1] if '/' in repo else repo
            original_title = entry.get('title', '新发布')
            
            # 在标题前添加仓库名称
            formatted_title = f"{repo_name} - {original_title}"
            
            entry_data = {
                "title": formatted_title,
                "link": entry.get('link', f'https://github.com/{repo}/releases'),
                "updated": updated_time,
                "author": author_name,
                "summary": summary,
                "repo": repo,
                "id": entry.get('id', entry.get('link', ''))
            }
            
            entries.append(entry_data)
        
        logger.debug(f"成功获取 {repo} 的最新发布条目" if entries else f"仓库 {repo} 没有发布条目")
        
    except requests.exceptions.Timeout:
        logger.error(f"获取 {repo} 超时")
    except requests.exceptions.ConnectionError:
        logger.error(f"连接 {repo} 失败")
    except requests.exceptions.RequestException as e:
        logger.error(f"请求 {repo} 时出错: {e}")
    except Exception as e:
        logger.error(f"处理 {repo} 时出现未知错误: {e}")
    
    return entries

def fetch_all_releases():
    """并发获取所有仓库的发布信息"""
    repos = load_repos()
    if not repos:
        return []
    
    all_entries = []
    successful_repos = 0
    
    logger.info(f"开始获取 {len(repos)} 个仓库的发布信息...")
    start_time = time.time()
    
    # 使用线程池并发请求
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 提交所有任务
        future_to_repo = {executor.submit(fetch_repo_releases, repo): repo for repo in repos}
        
        # 收集结果
        for future in concurrent.futures.as_completed(future_to_repo):
            repo = future_to_repo[future]
            try:
                entries = future.result()
                if entries:
                    all_entries.extend(entries)
                    successful_repos += 1
            except Exception as e:
                logger.error(f"处理 {repo} 的任务时出错: {e}")
    
    # 按时间排序
    all_entries.sort(key=lambda x: x["updated"], reverse=True)
    
    end_time = time.time()
    logger.info(f"完成! 成功处理 {successful_repos}/{len(repos)} 个仓库, "
                f"获取 {len(all_entries)} 个发布条目, 耗时 {end_time - start_time:.2f} 秒")
    
    return all_entries

def create_rss_feed(entries):
    """创建RSS 2.0格式的RSS Feed"""
    # 创建根元素
    rss = Element("rss")
    rss.set("version", "2.0")
    rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")
    
    # 创建channel元素
    channel = SubElement(rss, "channel")
    
    # RSS Feed元信息
    SubElement(channel, "title").text = "GitHub 仓库发布聚合"
    SubElement(channel, "description").text = f"聚合了 GitHub 仓库的最新发布信息，共 {len(entries)} 个条目"
    SubElement(channel, "link").text = "http://localhost:5000/rss"
    SubElement(channel, "language").text = "zh-cn"
    SubElement(channel, "lastBuildDate").text = formatdate(time.time(), usegmt=True)
    SubElement(channel, "pubDate").text = formatdate(time.time(), usegmt=True)
    SubElement(channel, "ttl").text = str(settings['cache_duration'] // 60)  # TTL in minutes
    
    # 添加生成器信息
    generator = SubElement(channel, "generator")
    generator.text = "GitHub RSS Aggregator v1.0"
    
    # 添加图标
    image = SubElement(channel, "image")
    SubElement(image, "url").text = "https://github.githubassets.com/favicons/favicon.png"
    SubElement(image, "title").text = "GitHub 仓库发布聚合"
    SubElement(image, "link").text = "http://localhost:5000/rss"
    SubElement(image, "width").text = "32"
    SubElement(image, "height").text = "32"
    
    # 添加自引用链接（Atom命名空间）
    atom_link = SubElement(channel, "atom:link")
    atom_link.set("href", "http://localhost:5000/rss")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")
    
    # 添加条目
    for item_data in entries[:MAX_ENTRIES]:
        item = SubElement(channel, "item")
        
        # 标题
        SubElement(item, "title").text = item_data["title"]
        
        # 链接
        SubElement(item, "link").text = item_data["link"]
        
        # GUID (唯一标识符)
        guid = SubElement(item, "guid")
        guid.text = item_data["id"]
        guid.set("isPermaLink", "false")
        
        # 发布时间 (RFC 822格式)
        SubElement(item, "pubDate").text = format_rfc822_date(item_data["updated"])
        
        # 作者
        if item_data["author"]:
            # RSS 2.0中作者字段应该是email格式，但GitHub不提供email
            # 所以我们使用dc:creator或者直接在description中提及
            SubElement(item, "author").text = f"noreply@github.com ({item_data['author']})"
        
        # 描述/摘要
        if item_data["summary"]:
            description = SubElement(item, "description")
            description.text = html.escape(item_data["summary"])
        
        # 分类 (仓库名)
        category = SubElement(item, "category")
        category.text = item_data["repo"]
        
        # 来源
        source = SubElement(item, "source")
        source.text = f"GitHub - {item_data['repo']}"
        source.set("url", f"https://github.com/{item_data['repo']}/releases.atom")
    
    # 格式化XML
    indent(rss, space="  ", level=0)
    
    return tostring(rss, encoding="utf-8", xml_declaration=True)

def get_cached_data():
    """获取缓存数据"""
    with cache['lock']:
        current_time = time.time()
        if (cache['data'] is not None and 
            current_time - cache['timestamp'] < settings['cache_duration']):
            logger.info("使用缓存数据")
            return cache['data']
        
        logger.info("缓存过期或不存在，重新获取数据")
        entries = fetch_all_releases()
        rss_xml = create_rss_feed(entries)
        
        # 更新缓存
        cache['data'] = rss_xml
        cache['timestamp'] = current_time
        
        return rss_xml

@app.route("/")
def index():
    """美化后的首页，包含编辑功能"""
    # 读取当前的配置
    current_token = load_github_token()
    
    repos_content = ""
    if os.path.exists(REPOS_FILE):
        try:
            with open(REPOS_FILE, "r", encoding="utf-8") as f:
                repos_content = f.read()
        except Exception:
            repos_content = ""
    
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <link rel="icon" href="https://github.githubassets.com/favicons/favicon.png">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>GitHub RSS 聚合器</title>
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            
            .container {
                background: rgba(255, 255, 255, 0.95);
                backdrop-filter: blur(10px);
                border-radius: 20px;
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.1);
                padding: 40px;
                max-width: 1200px;
                width: 100%;
                margin: 0 auto;
                border: 1px solid rgba(255, 255, 255, 0.2);
            }
            
            .header {
                text-align: center;
                margin-bottom: 30px;
            }
            
            .title {
                font-size: 2.5rem;
                font-weight: 700;
                color: #2d3748;
                margin-bottom: 10px;
                background: linear-gradient(135deg, #667eea, #764ba2);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
            }
            
            .subtitle {
                font-size: 1.1rem;
                color: #718096;
                margin-bottom: 30px;
                line-height: 1.6;
            }
            
            .content-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 30px;
                margin-bottom: 30px;
            }
            
            .config-section {
                background: linear-gradient(135deg, #f7fafc, #edf2f7);
                border-radius: 15px;
                padding: 25px;
                border: 1px solid rgba(255, 255, 255, 0.5);
                position: relative;
                overflow: hidden;
            }
            
            .config-section::before {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 3px;
                background: linear-gradient(90deg, #667eea, #764ba2);
            }
            
            .section-title {
                font-size: 1.3rem;
                font-weight: 600;
                color: #2d3748;
                margin-bottom: 15px;
                display: flex;
                align-items: center;
            }
            
            .section-icon {
                margin-right: 10px;
                font-size: 1.4rem;
            }
            
            .form-group {
                margin-bottom: 20px;
            }
            
            .form-label {
                display: block;
                font-weight: 500;
                color: #4a5568;
                margin-bottom: 8px;
                font-size: 0.9rem;
            }
            
            .form-input, .form-textarea {
                width: 100%;
                padding: 12px;
                border: 2px solid #e2e8f0;
                border-radius: 8px;
                font-size: 0.9rem;
                transition: all 0.3s ease;
                background: rgba(255, 255, 255, 0.8);
            }
            
            .input-with-button {
                position: relative;
                display: flex;
                align-items: center;
            }
            
            .input-with-button .form-input {
                padding-right: 45px;
            }
            
            .toggle-password-btn {
                position: absolute;
                right: 12px;
                background: none;
                border: none;
                cursor: pointer;
                padding: 5px;
                border-radius: 4px;
                transition: all 0.3s ease;
                font-size: 16px;
                line-height: 1;
                z-index: 10;
            }
            
            .toggle-password-btn:hover {
                background: rgba(102, 126, 234, 0.1);
                transform: scale(1.1);
            }
            
            .toggle-password-btn:active {
                transform: scale(0.95);
            }
            
            .form-input:focus, .form-textarea:focus {
                outline: none;
                border-color: #667eea;
                box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            }
            
            .form-textarea {
                height: 150px;
                resize: vertical;
                font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
                font-size: 0.85rem;
            }
            
            .btn {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                padding: 10px 20px;
                text-decoration: none;
                border-radius: 8px;
                font-weight: 600;
                font-size: 0.9rem;
                transition: all 0.3s ease;
                border: none;
                cursor: pointer;
                margin-right: 10px;
                margin-bottom: 10px;
            }
            
            .btn-primary {
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
                box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
            }
            
            .btn-primary:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(102, 126, 234, 0.6);
            }
            
            .btn-secondary {
                background: rgba(255, 255, 255, 0.8);
                color: #4a5568;
                border: 1px solid rgba(255, 255, 255, 0.5);
                backdrop-filter: blur(10px);
            }
            
            .btn-secondary:hover {
                background: rgba(255, 255, 255, 0.95);
                transform: translateY(-2px);
                box-shadow: 0 8px 20px rgba(0, 0, 0, 0.1);
            }
            
            .btn-icon {
                margin-right: 8px;
            }
            
            .actions-section {
                text-align: center;
                margin-bottom: 30px;
            }
            
            .actions-grid {
                display: grid;
                grid-template-columns: repeat(3, 1fr);
                gap: 15px;
                max-width: 800px;
                margin: 0 auto 20px auto;
            }
            
            .actions-grid-secondary {
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 15px;
                max-width: 500px;
                margin: 0 auto;
            }
            
            .btn-rss {
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
                box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
            }
            
            .btn-rss:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(102, 126, 234, 0.6);
            }
            
            .btn-refresh {
                background: linear-gradient(135deg, #48bb78, #38a169);
                color: white;
                box-shadow: 0 4px 15px rgba(72, 187, 120, 0.4);
            }
            
            .btn-refresh:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(72, 187, 120, 0.6);
            }
            
            .btn-status {
                background: linear-gradient(135deg, #4299e1, #3182ce);
                color: white;
                box-shadow: 0 4px 15px rgba(66, 153, 225, 0.4);
            }
            
            .btn-status:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(66, 153, 225, 0.6);
            }
            
            .btn-start {
                background: linear-gradient(135deg, #ed8936, #dd6b20);
                color: white;
                box-shadow: 0 4px 15px rgba(237, 137, 54, 0.4);
            }
            
            .btn-start:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(237, 137, 54, 0.6);
            }
            
            .btn-stop {
                background: linear-gradient(135deg, #f56565, #e53e3e);
                color: white;
                box-shadow: 0 4px 15px rgba(245, 101, 101, 0.4);
            }
            
            .btn-stop:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(245, 101, 101, 0.6);
            }
            
            .message {
                padding: 12px 20px;
                border-radius: 8px;
                margin-bottom: 20px;
                font-weight: 500;
                display: none;
            }
            
            .message.success {
                background: #c6f6d5;
                color: #22543d;
                border: 1px solid #9ae6b4;
            }
            
            .message.error {
                background: #fed7d7;
                color: #742a2a;
                border: 1px solid #feb2b2;
            }
            
            .help-text {
                font-size: 0.8rem;
                color: #718096;
                margin-top: 5px;
                line-height: 1.4;
            }
            
            .stats {
                background: rgba(255, 255, 255, 0.5);
                border-radius: 12px;
                padding: 20px;
                border: 1px solid rgba(255, 255, 255, 0.3);
                text-align: center;
            }
            
            .stats-title {
                font-size: 1rem;
                color: #4a5568;
                margin-bottom: 10px;
                font-weight: 500;
            }

            
            @media (max-width: 768px) {
                .container {
                    padding: 20px;
                    margin: 10px;
                }
                
                .title {
                    font-size: 2rem;
                }
                
                .content-grid {
                    grid-template-columns: 1fr;
                    gap: 20px;
                }
                
                .actions-grid {
                    grid-template-columns: 1fr;
                    gap: 10px;
                }
                
                .actions-grid-secondary {
                    grid-template-columns: 1fr;
                    gap: 10px;
                }
            }
            
            @keyframes fadeInUp {
                from {
                    opacity: 0;
                    transform: translateY(30px);
                }
                to {
                    opacity: 1;
                    transform: translateY(0);
                }
            }
            
            .container {
                animation: fadeInUp 0.8s ease-out;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 class="title">📡 GitHub RSS 聚合器</h1>
                <p class="subtitle">实时追踪您关注的 GitHub 仓库发布动态，一站式获取最新更新信息 (RSS 2.0)</p>
                

            </div>
            
            <div class="content-grid">
                <!-- GitHub Token 配置 -->
                <div class="config-section">
                    <h3 class="section-title">
                        <span class="section-icon">🔑</span>
                        GitHub Token 配置
                    </h3>
                    <form id="tokenForm">
                        <div class="form-group">
                            <label class="form-label" for="githubToken">GitHub Personal Access Token</label>
                            <div class="input-with-button">
                                <input type="password" id="githubToken" name="token" class="form-input" 
                                       value="{{ token_masked }}" placeholder="ghp_xxxxxxxxxxxxxxxxxxxx">
                                <button type="button" class="toggle-password-btn" onclick="toggleTokenVisibility()">
                                    <span id="toggleIcon">👁️</span>
                                </button>
                            </div>
                            <div class="help-text">
                                用于提高 GitHub API 访问限制。可在 GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens 创建。
                            </div>
                        </div>
                        <button type="submit" class="btn btn-primary">
                            <span class="btn-icon">💾</span>保存 Token
                        </button>
                    </form>
                </div>
                
                <!-- 仓库列表配置 -->
                <div class="config-section">
                    <h3 class="section-title">
                        <span class="section-icon">📦</span>
                        仓库列表配置
                    </h3>
                    <form id="reposForm">
                        <div class="form-group">
                            <label class="form-label" for="reposList">仓库列表（每行一个）</label>
                            <textarea id="reposList" name="repos" class="form-textarea" 
                                      placeholder="microsoft/vscode&#10;facebook/react&#10;microsoft/TypeScript">{{ repos_content }}</textarea>
                            <div class="help-text">
                                格式：用户名/仓库名，每行一个。支持 # 开头的注释行。
                            </div>
                        </div>
                        <button type="submit" class="btn btn-primary">
                            <span class="btn-icon">💾</span>保存仓库列表
                        </button>
                    </form>
                </div>
            </div>
            
            <!-- 消息提示 -->
            <div id="message" class="message"></div>
            
            <!-- 操作按钮 -->
            <div class="actions-section">
                <h3 style="margin-bottom: 20px; color: #4a5568;">🎛️ 服务操作</h3>
                
                <!-- 第一行：主要功能 -->
                <div class="actions-grid">
                    <a href="/rss" target="_blank" class="btn btn-rss">
                        <span class="btn-icon">📡</span>RSS Feed
                    </a>
                    <button onclick="refreshCache()" class="btn btn-refresh">
                        <span class="btn-icon">🔄</span>强制刷新
                    </button>
                    <a href="/status" class="btn btn-status">
                        <span class="btn-icon">📊</span>状态信息
                    </a>
                </div>
                
                <!-- 第二行：自动刷新控制 -->
                <div class="actions-grid-secondary">
                    <button onclick="startAutoRefresh()" class="btn btn-start">
                        <span class="btn-icon">▶️</span>启动自动刷新
                    </button>
                    <button onclick="stopAutoRefresh()" class="btn btn-stop">
                        <span class="btn-icon">⏹️</span>停止自动刷新
                    </button>
                </div>
            </div>
            
            <div class="stats">
                <div class="stats-title">🎯 服务特性</div>
                <div style="color: #718096; font-size: 0.9rem; line-height: 1.6;">
                    支持并发处理 • 智能错误处理 • RSS 2.0 格式输出 • 自定义限制条目数 • Web 界面配置 • 动态缓存管理
                </div>
            </div>
        </div>

        <script>
            function showMessage(text, type = 'success') {
                const message = document.getElementById('message');
                message.textContent = text;
                message.className = `message ${type}`;
                message.style.display = 'block';
                
                setTimeout(() => {
                    message.style.display = 'none';
                }, 5000);
            }
            
            // 切换Token显示/隐藏
            function toggleTokenVisibility() {
                const tokenInput = document.getElementById('githubToken');
                const toggleIcon = document.getElementById('toggleIcon');
                
                if (tokenInput.type === 'password') {
                    tokenInput.type = 'text';
                    toggleIcon.textContent = '🙈';
                } else {
                    tokenInput.type = 'password';
                    toggleIcon.textContent = '👁️';
                }
            }
            
            // Token 表单提交
            document.getElementById('tokenForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                const formData = new FormData(e.target);
                
                try {
                    const response = await fetch('/save_token', {
                        method: 'POST',
                        body: formData
                    });
                    
                    const result = await response.json();
                    
                    if (response.ok) {
                        showMessage('GitHub Token 保存成功！', 'success');
                    } else {
                        showMessage(result.error || '保存失败', 'error');
                    }
                } catch (error) {
                    showMessage('网络错误：' + error.message, 'error');
                }
            });
            
            // 仓库列表表单提交
            document.getElementById('reposForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                const formData = new FormData(e.target);
                
                try {
                    const response = await fetch('/save_repos', {
                        method: 'POST',
                        body: formData
                    });
                    
                    const result = await response.json();
                    
                    if (response.ok) {
                        showMessage('仓库列表保存成功！', 'success');
                    } else {
                        showMessage(result.error || '保存失败', 'error');
                    }
                } catch (error) {
                    showMessage('网络错误：' + error.message, 'error');
                }
            });
            
            // 刷新缓存
            async function refreshCache() {
                try {
                    const response = await fetch('/refresh');
                    const result = await response.json();
                    showMessage(result.message, 'success');
                } catch (error) {
                    showMessage('刷新失败：' + error.message, 'error');
                }
            }
            
            // 停止自动刷新
            async function stopAutoRefresh() {
                try {
                    const response = await fetch('/stop_auto_refresh');
                    const result = await response.json();
                    showMessage(result.message, 'success');
                } catch (error) {
                    showMessage('操作失败：' + error.message, 'error');
                }
            }
            
            // 启动自动刷新
            async function startAutoRefresh() {
                try {
                    const response = await fetch('/start_auto_refresh');
                    const result = await response.json();
                    showMessage(result.message, 'success');
                } catch (error) {
                    showMessage('操作失败：' + error.message, 'error');
                }
            }
        </script>
    </body>
    </html>
    """, token_masked="*" * 8 if current_token else "", repos_content=repos_content)

@app.route("/save_token", methods=["POST"])
def save_token():
    """保存GitHub Token"""
    try:
        token = request.form.get("token", "").strip()
        
        if save_github_token(token):
            # 清空缓存，下次请求时会使用新的token
            with cache['lock']:
                cache['data'] = None
                cache['timestamp'] = 0
            
            return jsonify({"message": "GitHub Token 保存成功"})
        else:
            return jsonify({"error": "保存失败"}), 500
    except Exception as e:
        logger.error(f"保存token时出错: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/save_repos", methods=["POST"])
def save_repos_route():
    """保存仓库列表"""
    try:
        repos_text = request.form.get("repos", "").strip()
        
        if save_repos(repos_text):
            # 清空缓存，下次请求时会使用新的仓库列表
            with cache['lock']:
                cache['data'] = None
                cache['timestamp'] = 0
            
            return jsonify({"message": "仓库列表保存成功"})
        else:
            return jsonify({"error": "保存失败"}), 500
    except Exception as e:
        logger.error(f"保存仓库列表时出错: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/save_settings", methods=["POST"])
def save_settings_route():
    """保存系统设置"""
    try:
        cache_duration = int(request.form.get("cache_duration", settings['cache_duration']))
        auto_refresh_interval = int(request.form.get("auto_refresh_interval", settings['auto_refresh_interval']))
        startup_refresh = request.form.get("startup_refresh", "off") == "on"
        
        # 验证设置范围
        if not (30 <= cache_duration <= 3600):  # 30秒到1小时
            return jsonify({"error": "缓存持续时间必须在30秒到3600秒之间"}), 400
            
        if not (60 <= auto_refresh_interval <= 86400):  # 1分钟到24小时
            return jsonify({"error": "自动刷新间隔必须在60秒到86400秒之间"}), 400
        
        # 更新设置
        old_auto_refresh_interval = settings['auto_refresh_interval']
        settings['cache_duration'] = cache_duration
        settings['auto_refresh_interval'] = auto_refresh_interval
        settings['startup_refresh'] = startup_refresh
        
        if save_settings():
            # 清空缓存，应用新的缓存设置
            with cache['lock']:
                cache['data'] = None
                cache['timestamp'] = 0
            
            # 立即执行强制刷新
            logger.info("设置保存后执行强制刷新...")
            try:
                entries = fetch_all_releases()
                rss_xml = create_rss_feed(entries)
                
                # 更新缓存
                with cache['lock']:
                    cache['data'] = rss_xml
                    cache['timestamp'] = time.time()
                
                logger.info(f"强制刷新完成，获取到 {len(entries)} 个发布条目")
            except Exception as e:
                logger.error(f"设置保存后强制刷新失败: {e}")
            
            # 如果自动刷新间隔改变了，重启自动刷新服务
            if old_auto_refresh_interval != auto_refresh_interval:
                restart_auto_refresh()
            
            return jsonify({
                "message": "系统设置保存成功",
                "cache_duration": cache_duration,
                "auto_refresh_interval": auto_refresh_interval,
                "startup_refresh": startup_refresh
            })
        else:
            return jsonify({"error": "保存失败"}), 500
    except ValueError:
        return jsonify({"error": "请输入有效的数字"}), 400
    except Exception as e:
        logger.error(f"保存系统设置时出错: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/rss")
def aggregate_rss():
    """主要的RSS聚合接口"""
    try:
        # 使用缓存机制
        rss_xml = get_cached_data()
        
        # 处理limit参数
        limit = int(request.args.get("limit", MAX_ENTRIES))
        if limit != MAX_ENTRIES and limit < MAX_ENTRIES:
            # 如果请求的limit小于默认值，需要重新生成
            entries = fetch_all_releases()
            limited_entries = entries[:min(limit, 500)]
            rss_xml = create_rss_feed(limited_entries)
        
        return Response(rss_xml, content_type="application/rss+xml; charset=utf-8", headers={
            "Cache-Control": f"public, max-age={settings['cache_duration']}"
        })
    except Exception as e:
        logger.error(f"生成RSS时出错: {e}")
        return Response("RSS生成失败", status=500)

@app.route("/status")
def status():
    """状态信息接口"""
    repos = load_repos()
    token = load_github_token()
    cache_age = time.time() - cache['timestamp'] if cache['timestamp'] > 0 else -1
    
    # 如果请求JSON格式，返回原始数据
    if request.headers.get('Accept', '').find('application/json') != -1 or request.args.get('format') == 'json':
        status_info = {
            "repos_count": len(repos),
            "has_github_token": bool(token),
            "cache_age_seconds": round(cache_age, 2),
            "cache_valid": cache_age < settings['cache_duration'] if cache_age >= 0 else False,
            "last_update": datetime.fromtimestamp(cache['timestamp']).isoformat() if cache['timestamp'] > 0 else None,
            "max_entries": MAX_ENTRIES,
            "request_timeout": REQUEST_TIMEOUT,
            "max_workers": MAX_WORKERS,
            "cache_duration": settings['cache_duration'],
            "auto_refresh_interval": settings['auto_refresh_interval'],
            "auto_refresh_running": auto_refresh_running,
            "startup_refresh_enabled": STARTUP_REFRESH and settings.get('startup_refresh', True),
            "format": "RSS 2.0"
        }
        return jsonify(status_info)
    
    # 否则返回格式化的HTML页面
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <link rel="icon" href="https://github.githubassets.com/favicons/favicon.png">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>系统状态 - GitHub RSS 聚合器</title>
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding: 20px;
            }
            
            .container {
                background: rgba(255, 255, 255, 0.95);
                backdrop-filter: blur(10px);
                border-radius: 20px;
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.1);
                padding: 40px;
                max-width: 1200px;
                width: 100%;
                margin: 0 auto;
                border: 1px solid rgba(255, 255, 255, 0.2);
            }
            
            .header {
                text-align: center;
                margin-bottom: 40px;
            }
            
            .title {
                font-size: 2.5rem;
                font-weight: 700;
                color: #2d3748;
                margin-bottom: 10px;
                background: linear-gradient(135deg, #667eea, #764ba2);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
            }
            
            .subtitle {
                font-size: 1.1rem;
                color: #718096;
                margin-bottom: 20px;
            }
            
            .status-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
                gap: 25px;
                margin-bottom: 30px;
            }
            
            .status-card {
                background: linear-gradient(135deg, #f7fafc, #edf2f7);
                border-radius: 15px;
                padding: 25px;
                border: 1px solid rgba(255, 255, 255, 0.5);
                position: relative;
                overflow: hidden;
            }
            
            .status-card::before {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 3px;
                background: linear-gradient(90deg, #667eea, #764ba2);
            }
            
            .card-title {
                font-size: 1.2rem;
                font-weight: 600;
                color: #2d3748;
                margin-bottom: 15px;
                display: flex;
                align-items: center;
            }
            
            .card-icon {
                margin-right: 10px;
                font-size: 1.3rem;
            }
            
            .status-item {
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 10px 0;
                border-bottom: 1px solid rgba(0, 0, 0, 0.1);
            }
            
            .status-item:last-child {
                border-bottom: none;
            }
            
            .status-label {
                font-weight: 500;
                color: #4a5568;
                font-size: 0.9rem;
            }
            
            .status-value {
                font-weight: 600;
                color: #2d3748;
                font-size: 0.9rem;
            }
            
            .status-badge {
                padding: 4px 12px;
                border-radius: 20px;
                font-size: 0.8rem;
                font-weight: 600;
            }
            
            .badge-success {
                background: #c6f6d5;
                color: #22543d;
            }
            
            .badge-warning {
                background: #feebc8;
                color: #744210;
            }
            
            .badge-error {
                background: #fed7d7;
                color: #742a2a;
            }
            
            .badge-info {
                background: #bee3f8;
                color: #2a4365;
            }
            
            .settings-card {
                background: linear-gradient(135deg, #f0fff4, #e6fffa);
                border: 2px solid #38b2ac;
            }
            
            .settings-card::before {
                background: linear-gradient(90deg, #38b2ac, #319795);
            }
            
            .form-group {
                margin-bottom: 15px;
            }
            
            .form-label {
                display: block;
                font-weight: 500;
                color: #4a5568;
                margin-bottom: 8px;
                font-size: 0.9rem;
            }
            
            .form-input {
                width: 100%;
                padding: 10px;
                border: 2px solid #e2e8f0;
                border-radius: 8px;
                font-size: 0.9rem;
                transition: all 0.3s ease;
                background: rgba(255, 255, 255, 0.8);
            }
            
            .form-input:focus {
                outline: none;
                border-color: #38b2ac;
                box-shadow: 0 0 0 3px rgba(56, 178, 172, 0.1);
            }
            
            .form-checkbox {
                margin-right: 8px;
            }
            
            .checkbox-group {
                display: flex;
                align-items: center;
                margin-bottom: 15px;
            }
            
            .help-text {
                font-size: 0.8rem;
                color: #718096;
                margin-top: 5px;
                line-height: 1.4;
            }
            
            .btn {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                padding: 12px 24px;
                text-decoration: none;
                border-radius: 12px;
                font-weight: 600;
                font-size: 0.95rem;
                transition: all 0.3s ease;
                border: none;
                cursor: pointer;
                margin: 0 10px;
            }
            
            .btn-primary {
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
                box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
            }
            
            .btn-primary:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(102, 126, 234, 0.6);
            }
            
            .btn-secondary {
                background: rgba(255, 255, 255, 0.8);
                color: #4a5568;
                border: 1px solid rgba(255, 255, 255, 0.5);
                backdrop-filter: blur(10px);
            }
            
            .btn-secondary:hover {
                background: rgba(255, 255, 255, 0.95);
                transform: translateY(-2px);
                box-shadow: 0 8px 20px rgba(0, 0, 0, 0.1);
            }
            
            .btn-settings {
                background: linear-gradient(135deg, #38b2ac, #319795);
                color: white;
                box-shadow: 0 4px 15px rgba(56, 178, 172, 0.4);
            }
            
            .btn-settings:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(56, 178, 172, 0.6);
            }
            
            /* 新增按钮颜色样式 */
            .btn-home {
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: white;
                box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
            }
            
            .btn-home:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(102, 126, 234, 0.6);
            }
            
            .btn-rss {
                background: linear-gradient(135deg, #ed8936, #dd6b20);
                color: white;
                box-shadow: 0 4px 15px rgba(237, 137, 54, 0.4);
            }
            
            .btn-rss:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(237, 137, 54, 0.6);
            }
            
            .btn-refresh {
                background: linear-gradient(135deg, #48bb78, #38a169);
                color: white;
                box-shadow: 0 4px 15px rgba(72, 187, 120, 0.4);
            }
            
            .btn-refresh:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 25px rgba(72, 187, 120, 0.6);
            }
            
            .btn-icon {
                margin-right: 8px;
            }
            
            .actions {
                text-align: center;
                margin-top: 30px;
            }
            
            .message {
                padding: 12px 20px;
                border-radius: 8px;
                margin-bottom: 20px;
                font-weight: 500;
                display: none;
            }
            
            .message.success {
                background: #c6f6d5;
                color: #22543d;
                border: 1px solid #9ae6b4;
            }
            
            .message.error {
                background: #fed7d7;
                color: #742a2a;
                border: 1px solid #feb2b2;
            }
            
            @media (max-width: 768px) {
                .container {
                    padding: 20px;
                    margin: 10px;
                }
                
                .title {
                    font-size: 2rem;
                }
                
                .status-grid {
                    grid-template-columns: 1fr;
                }
            }
            
            @keyframes fadeInUp {
                from {
                    opacity: 0;
                    transform: translateY(30px);
                }
                to {
                    opacity: 1;
                    transform: translateY(0);
                }
            }
            
            .container {
                animation: fadeInUp 0.8s ease-out;
            }
            
            .last-update {
                text-align: center;
                font-size: 0.85rem;
                color: #718096;
                margin-top: 20px;
                padding: 15px;
                background: rgba(255, 255, 255, 0.5);
                border-radius: 10px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 class="title">📊 系统状态</h1>
                <p class="subtitle">GitHub RSS 聚合器运行状态监控与配置管理</p>
                <div class="last-update">
                    页面刷新时间：{{ current_time }}
                </div>
            </div>
            
            <!-- 消息提示 -->
            <div id="message" class="message"></div>
            
            <div class="status-grid">
                <!-- 基础配置 -->
                <div class="status-card">
                    <h3 class="card-title">
                        <span class="card-icon">⚙️</span>
                        基础配置
                    </h3>
                    <div class="status-item">
                        <span class="status-label">监控仓库数量</span>
                        <span class="status-value">{{ repos_count }} 个</span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">GitHub Token</span>
                        <span class="status-badge {{ 'badge-success' if has_token else 'badge-warning' }}">
                            {{ '已配置' if has_token else '未配置' }}
                        </span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">最大条目数</span>
                        <span class="status-value">{{ max_entries }} 条</span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">输出格式</span>
                        <span class="status-badge badge-info">RSS 2.0</span>
                    </div>
                </div>
                
                <!-- 性能参数 -->
                <div class="status-card">
                    <h3 class="card-title">
                        <span class="card-icon">🚀</span>
                        性能参数
                    </h3>
                    <div class="status-item">
                        <span class="status-label">请求超时时间</span>
                        <span class="status-value">{{ request_timeout }} 秒</span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">最大并发数</span>
                        <span class="status-value">{{ max_workers }} 个</span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">缓存持续时间</span>
                        <span class="status-value">{{ cache_duration }} 秒</span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">自动刷新间隔</span>
                        <span class="status-value">{{ auto_refresh_interval }} 秒</span>
                    </div>
                </div>
                
                <!-- 缓存状态 -->
                <div class="status-card">
                    <h3 class="card-title">
                        <span class="card-icon">💾</span>
                        缓存状态
                    </h3>
                    <div class="status-item">
                        <span class="status-label">缓存状态</span>
                        <span class="status-badge {{ 'badge-success' if cache_valid else 'badge-warning' }}">
                            {{ '有效' if cache_valid else '已过期' }}
                        </span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">缓存年龄</span>
                        <span class="status-value">{{ cache_age_display }}</span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">最后更新</span>
                        <span class="status-value">{{ last_update_display }}</span>
                    </div>
                </div>
                
                <!-- 自动刷新 -->
                <div class="status-card">
                    <h3 class="card-title">
                        <span class="card-icon">🔄</span>
                        自动刷新
                    </h3>
                    <div class="status-item">
                        <span class="status-label">自动刷新状态</span>
                        <span class="status-badge {{ 'badge-success' if auto_refresh_running else 'badge-error' }}">
                            {{ '运行中' if auto_refresh_running else '已停止' }}
                        </span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">刷新间隔</span>
                        <span class="status-value">{{ auto_refresh_interval }} 秒</span>
                    </div>
                    <div class="status-item">
                        <span class="status-label">启动时刷新</span>
                        <span class="status-badge {{ 'badge-success' if startup_refresh_enabled else 'badge-warning' }}">
                            {{ '已启用' if startup_refresh_enabled else '已禁用' }}
                        </span>
                    </div>
                </div>
                
                <!-- 动态设置配置 -->
                <div class="status-card settings-card">
                    <h3 class="card-title">
                        <span class="card-icon">🎛️</span>
                        动态设置配置
                    </h3>
                    <form id="settingsForm">
                        <div class="form-group">
                            <label class="form-label" for="cacheDuration">缓存持续时间（秒）</label>
                            <input type="number" id="cacheDuration" name="cache_duration" class="form-input" 
                                   value="{{ cache_duration }}" min="30" max="3600" step="30">
                            <div class="help-text">
                                范围：30-3600秒。缓存有效期，过期后会重新获取数据。
                            </div>
                        </div>
                        <div class="form-group">
                            <label class="form-label" for="autoRefreshInterval">自动刷新间隔（秒）</label>
                            <input type="number" id="autoRefreshInterval" name="auto_refresh_interval" class="form-input" 
                                   value="{{ auto_refresh_interval }}" min="60" max="86400" step="60">
                            <div class="help-text">
                                范围：60-86400秒。自动后台刷新缓存的间隔时间。
                            </div>
                        </div>
                        <div class="checkbox-group">
                            <input type="checkbox" id="startupRefresh" name="startup_refresh" class="form-checkbox" 
                                   {{ 'checked' if startup_refresh_enabled else '' }}>
                            <label class="form-label" for="startupRefresh" style="margin-bottom: 0;">启用启动时自动刷新</label>
                        </div>
                        <div class="help-text" style="margin-bottom: 15px;">
                            启用后容器启动时会自动执行一次强制刷新。也可通过环境变量 STARTUP_REFRESH 控制。
                        </div>
                        <button type="submit" class="btn btn-settings">
                            <span class="btn-icon">💾</span>保存设置
                        </button>
                    </form>
                </div>
            </div>
            
            <div class="actions">
                <a href="/" class="btn btn-home">
                    <span class="btn-icon">🏠</span>返回首页
                </a>
                <a href="/rss" target="_blank" class="btn btn-rss">
                    <span class="btn-icon">📡</span>RSS Feed
                </a>
                <button onclick="refreshCache()" class="btn btn-refresh">
                    <span class="btn-icon">🔄</span>强制刷新
                </button>
            </div>
        </div>
        
        <script>
            function showMessage(text, type = 'success') {
                const message = document.getElementById('message');
                message.textContent = text;
                message.className = `message ${type}`;
                message.style.display = 'block';
                
                setTimeout(() => {
                    message.style.display = 'none';
                }, 5000);
            }
            
            // 刷新缓存
            async function refreshCache() {
                try {
                    const response = await fetch('/refresh');
                    const result = await response.json();
                    showMessage(result.message, 'success');
                } catch (error) {
                    showMessage('刷新失败：' + error.message, 'error');
                }
            }
            
            // 设置表单提交
            document.getElementById('settingsForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                const formData = new FormData(e.target);
                
                try {
                    const response = await fetch('/save_settings', {
                        method: 'POST',
                        body: formData
                    });
                    
                    const result = await response.json();
                    
                    if (response.ok) {
                        showMessage('系统设置保存成功！缓存已清空，自动刷新服务已更新。', 'success');
                        // 延迟刷新页面以显示新的设置值
                        setTimeout(() => {
                            window.location.reload();
                        }, 2000);
                    } else {
                        showMessage(result.error || '保存失败', 'error');
                    }
                } catch (error) {
                    showMessage('网络错误：' + error.message, 'error');
                }
            });
        </script>
    </body>
    </html>
    """, 
    repos_count=len(repos),
    has_token=bool(token),
    cache_valid=cache_age < settings['cache_duration'] if cache_age >= 0 else False,
    cache_age_display=f"{cache_age:.1f} 秒" if cache_age >= 0 else "无缓存",
    last_update_display=datetime.fromtimestamp(cache['timestamp']).strftime('%Y-%m-%d %H:%M:%S') if cache['timestamp'] > 0 else "暂无",
    max_entries=MAX_ENTRIES,
    request_timeout=REQUEST_TIMEOUT,
    max_workers=MAX_WORKERS,
    cache_duration=settings['cache_duration'],
    auto_refresh_interval=settings['auto_refresh_interval'],
    auto_refresh_running=auto_refresh_running,
    startup_refresh_enabled=STARTUP_REFRESH and settings.get('startup_refresh', True),
    current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    )

@app.route("/refresh")
def refresh():
    """强制刷新缓存"""
    try:
        with cache['lock']:
            cache['data'] = None
            cache['timestamp'] = 0
        
        logger.info("缓存已清空，正在重新获取数据...")
        
        # 立即重新加载数据
        entries = fetch_all_releases()
        rss_xml = create_rss_feed(entries)
        
        # 更新缓存
        with cache['lock']:
            cache['data'] = rss_xml
            cache['timestamp'] = time.time()
        
        logger.info("缓存已刷新并重新加载数据")
        return jsonify({"message": f"缓存已刷新成功！获取到 {len(entries)} 个发布条目"})
    except Exception as e:
        logger.error(f"刷新缓存时出错: {e}")
        return jsonify({"error": f"刷新失败: {str(e)}"}), 500

@app.route("/stop_auto_refresh")
def stop_auto_refresh():
    """停止自动刷新"""
    global auto_refresh_running
    auto_refresh_running = False
    logger.info("自动刷新已停止")
    return jsonify({"message": "自动刷新已停止"})

@app.route("/start_auto_refresh")
def start_auto_refresh_route():
    """启动自动刷新"""
    global auto_refresh_running, refresh_thread
    
    # 如果已经在运行，先停止
    if auto_refresh_running:
        auto_refresh_running = False
        if refresh_thread and refresh_thread.is_alive():
            time.sleep(1)  # 等待线程自然结束
    
    # 启动新的自动刷新线程
    refresh_thread = start_auto_refresh()
    logger.info("自动刷新已启动")
    return jsonify({"message": f"自动刷新已启动！刷新间隔：{settings['auto_refresh_interval']} 秒"})

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "页面未找到"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "服务器内部错误"}), 500

if __name__ == "__main__":
    # 加载系统设置
    load_settings()
    
    logger.info("启动 GitHub RSS 聚合器...")
    logger.info(f"最大条目数: {MAX_ENTRIES}")
    logger.info(f"请求超时: {REQUEST_TIMEOUT}秒")
    logger.info(f"最大并发数: {MAX_WORKERS}")
    logger.info(f"缓存持续时间: {settings['cache_duration']}秒")
    logger.info(f"自动刷新间隔: {settings['auto_refresh_interval']}秒")
    logger.info(f"启动时刷新: {STARTUP_REFRESH and settings.get('startup_refresh', True)}")
    logger.info("输出格式: RSS 2.0")
    
    # 启动时预热缓存
    startup_cache_warmup()
    
    # 启动自动刷新后台任务
    refresh_thread = start_auto_refresh()
    
    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")
        auto_refresh_running = False
        logger.info("程序已关闭")