from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.svg', '.avif', '.bmp'}
DOC_EXTS = {'.pdf', '.ppt', '.pptx', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.rar', '.7z'}
VIDEO_EXTS = {'.mp4', '.mov', '.webm', '.m4v'}

CATEGORY_RULES = [
    ('影视广电与虚拟拍摄', r'影视|广电|电视台|演播|直播|虚拟|XR|VP|摄影棚|拍摄|电影|studio|NAB|IBC'),
    ('广告传媒与户外显示', r'广告|传媒|户外|商显|裸眼|3D|地标|商业显示|DOOH|车载屏|橱窗'),
    ('体育场馆', r'体育|场馆|赛事|足球|篮球|排球|马拉松|球场|世界杯|欧洲杯|超级杯|NBL|MLB'),
    ('交通枢纽', r'机场|地铁|高铁|车站|交通|航站楼|铁路|轨道'),
    ('金融网点', r'银行|金融|网点|建行|工行|农商行|邮政|营业厅|区块链'),
    ('政企会议与指挥中心', r'政企|会议|控制室|指挥|调度|报告厅|政府|公安|会议室|一体机'),
    ('教育与医疗', r'教育|学校|大学|校园|教学|培训|医疗|医院'),
    ('零售商业', r'零售|门店|商场|百货|展厅|商业综合体|新零售'),
    ('租赁舞台与展会', r'租赁|舞台|演出|展会|ISE|InfoComm|ISLE|LDI|展览'),
    ('夜游文旅与照明', r'夜景|照明|文旅|城市|景观|灯光'),
]

ENTRY_DIRS = {
    'product': '01_产品资料',
    'case': '02_案例集',
    'solution': '03_解决方案',
    'marketing': '04_营销资料',
    'testimonial': '05_客户证言视频',
}

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 AOTO-Structured-CN-Crawler/1.0',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.4',
})


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def slug(text: str, fallback: str = '未命名') -> str:
    value = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]+', '-', clean(text)).strip('-').lower()
    return value[:90] or fallback


def digest(text: str, n: int = 12) -> str:
    return hashlib.sha256((text or '').encode('utf-8', errors='ignore')).hexdigest()[:n]


def normalize(url: str, base: str = '') -> str:
    if not url:
        return ''
    url = url.strip()
    if url.startswith(('data:', 'mailto:', 'tel:', 'javascript:')):
        return ''
    absolute = urljoin(base, url)
    absolute, _ = urldefrag(absolute)
    return absolute


def ext(url: str) -> str:
    path = urlparse(url).path.lower()
    return '.' + path.rsplit('.', 1)[-1] if '.' in path else ''


def is_asset(url: str) -> bool:
    return ext(url) in IMAGE_EXTS | DOC_EXTS | VIDEO_EXTS


def asset_type(url: str, content_type: str = '') -> str:
    e = ext(url)
    ct = content_type.lower()
    if e in IMAGE_EXTS or ct.startswith('image/'):
        return 'image'
    if e in DOC_EXTS or any(x in ct for x in ['pdf', 'powerpoint', 'zip', 'msword', 'excel']):
        return 'document'
    if e in VIDEO_EXTS or ct.startswith('video/'):
        return 'video'
    return 'link'


def title_of(soup: BeautifulSoup, url: str) -> str:
    for sel in ['h1', "meta[property='og:title']", 'title']:
        tag = soup.select_one(sel)
        if tag:
            val = tag.get('content') if tag.name == 'meta' else tag.get_text(' ', strip=True)
            val = clean(val).replace(' - AOTO', '').replace(' – AOTO', '')
            if val:
                return val
    return urlparse(url).path.strip('/') or url


def classify_category(text: str) -> str:
    for label, pattern in CATEGORY_RULES:
        if re.search(pattern, text, re.I):
            return label
    return '其他'


def classify_entity(url: str, title: str, text: str) -> str | None:
    bag = f'{url} {title} {text[:1800]}'
    u = url.lower()
    if re.search(r'/product|/products|产品中心|产品参数|规格参数|像素间距', bag, re.I):
        if not re.search(r'/company-news|/market-activities', u):
            return 'product'
    if re.search(r'客户证言|客户采访|客户评价|testimonial|interview|视频案例', bag, re.I):
        return 'testimonial'
    if re.search(r'/case|/cases|案例|项目案例|应用案例|成功案例', bag, re.I):
        return 'case'
    if re.search(r'/solution|解决方案|行业方案|场景方案|综合解决方案', bag, re.I):
        return 'solution'
    if re.search(r'白皮书|资料下载|下载资料|彩页|画册|公司介绍|宣传册|营销资料|展会资料', bag, re.I):
        return 'marketing'
    # 新闻里如果明显是项目落地，作为案例候选保存；普通新闻不入结构化包
    if re.search(r'/company-news|/market-activities', u) and re.search(r'项目|案例|助力|打造|部署|落地|采用|中标|选用|亮相|应用于|交付|机场|银行|场馆|电视台|演播厅|网点', bag, re.I):
        return 'case'
    return None


def paragraphs(soup: BeautifulSoup) -> list[str]:
    values = []
    for tag in soup.find_all(['p', 'li']):
        tx = clean(tag.get_text(' ', strip=True))
        if len(tx) >= 12 and tx not in values:
            values.append(tx)
    return values


def description_of(soup: BeautifulSoup, text: str) -> str:
    meta = soup.select_one("meta[name='description']") or soup.select_one("meta[property='og:description']")
    if meta and clean(meta.get('content', '')):
        return clean(meta.get('content', ''))
    ps = [p for p in paragraphs(soup) if len(p) >= 20]
    return '\n'.join(ps[:5]) or clean(text[:500])


def selling_points(soup: BeautifulSoup) -> list[dict]:
    points = []
    for h in soup.find_all(['h2', 'h3', 'h4']):
        title = clean(h.get_text(' ', strip=True))
        if not title or len(title) > 90:
            continue
        body_parts = []
        for sib in h.find_all_next():
            if sib.name in ['h2', 'h3', 'h4']:
                break
            if sib.name in ['p', 'li']:
                tx = clean(sib.get_text(' ', strip=True))
                if tx:
                    body_parts.append(tx)
            if len(' '.join(body_parts)) > 520:
                break
        body = clean(' '.join(body_parts))
        if body and 15 <= len(body) <= 700:
            if re.search(r'特点|优势|亮点|功能|技术|刷新|亮度|对比|色域|防护|维护|安装|节能|像素|Hz|HDR|Mini|Micro|COB|MIP|封装|可靠|智能', title + body, re.I):
                points.append({'title': title, 'description': body[:700]})
    if points:
        return points[:24]
    # 没有明显标题时，用段落里的特征句兜底
    for p in paragraphs(soup):
        if re.search(r'采用|支持|具有|具备|可实现|可满足|高亮|高清|节能|防护|维护|安装|可靠|稳定|智能|刷新|亮度|对比', p):
            points.append({'title': '产品/项目卖点', 'description': p[:700]})
        if len(points) >= 12:
            break
    return points


def asset_links(base_url: str, soup: BeautifulSoup) -> list[tuple[str, str]]:
    out = []
    for img in soup.find_all('img'):
        for attr in ['src', 'data-src', 'data-original', 'data-lazy-src']:
            u = normalize(img.get(attr, ''), base_url)
            if u:
                out.append((u, clean(img.get('alt', ''))))
        srcset = img.get('srcset') or img.get('data-srcset')
        if srcset:
            for part in srcset.split(','):
                u = normalize(part.strip().split(' ')[0], base_url)
                if u:
                    out.append((u, clean(img.get('alt', ''))))
    for sel in ["meta[property='og:image']", "meta[name='twitter:image']"]:
        tag = soup.select_one(sel)
        if tag and tag.get('content'):
            out.append((normalize(tag.get('content'), base_url), '封面图'))
    for a in soup.find_all('a', href=True):
        u = normalize(a.get('href'), base_url)
        if is_asset(u):
            out.append((u, clean(a.get_text(' ', strip=True))))
    seen = set()
    result = []
    for u, label in out:
        if u and u not in seen:
            seen.add(u)
            result.append((u, label))
    return result


def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or '', encoding='utf-8', errors='ignore')


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def download_asset(asset_url: str, label: str, page_url: str, target_folder: Path, max_mb: int = 80):
    try:
        r = session.get(asset_url, timeout=45, verify=False, allow_redirects=True)
        r.raise_for_status()
        body = r.content or b''
        if not body or len(body) > max_mb * 1024 * 1024:
            return None
        k = asset_type(r.url, r.headers.get('Content-Type', ''))
        e = ext(r.url) or ('.jpg' if k == 'image' else '.mp4' if k == 'video' else '.bin')
        sub = 'images' if k == 'image' else 'downloads' if k == 'document' else 'videos'
        name = f"{slug(label or Path(urlparse(r.url).path).stem or k)}-{digest(r.url, 8)}{e}"
        folder = target_folder / sub
        folder.mkdir(parents=True, exist_ok=True)
        local = folder / name
        local.write_bytes(body)
        return {'sourceUrl': r.url, 'label': label, 'assetType': k, 'fileName': name, 'localPath': str(local), 'sizeBytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(), 'pageUrl': page_url}
    except Exception as exc:
        return {'sourceUrl': asset_url, 'label': label, 'assetType': 'failed', 'error': repr(exc), 'pageUrl': page_url}


def main():
    out = Path(os.getenv('AOTO_STRUCTURED_OUT', 'aoto-official-cn-structured')).resolve()
    seeds = [u.strip() for u in os.getenv('AOTO_SEEDS', 'https://www.aoto.com/').split(',') if u.strip()]
    allowed = {d.strip().lower() for d in os.getenv('AOTO_ALLOWED_DOMAINS', 'www.aoto.com,aoto.com').split(',') if d.strip()}
    max_pages = int(os.getenv('AOTO_MAX_PAGES', '0') or '0')
    download_images = os.getenv('AOTO_DOWNLOAD_IMAGES', '1') != '0'
    download_docs = os.getenv('AOTO_DOWNLOAD_DOCS', '1') != '0'
    download_videos = os.getenv('AOTO_DOWNLOAD_VIDEOS', '0') == '1'
    max_mb = int(os.getenv('AOTO_MAX_FILE_MB', '80') or '80')

    for d in ['00_爬取报告', '01_产品资料', '02_案例集', '03_解决方案', '04_营销资料', '05_客户证言视频', '06_文件资产索引', '99_未入库页面']:
        (out / d).mkdir(parents=True, exist_ok=True)

    queue = deque(seeds)
    visited = set()
    entities = []
    assets = []
    failed = []
    ignored = []

    while queue:
        url = normalize(queue.popleft())
        if not url or url in visited:
            continue
        if max_pages and len(visited) >= max_pages:
            break
        host = urlparse(url).netloc.lower().split(':')[0]
        if host not in allowed:
            continue
        if is_asset(url):
            continue
        if re.search(r'/wp-admin|/login|/cart|/checkout|/feed/|\?s=', url, re.I):
            continue
        visited.add(url)
        print(f'[扫描] {len(visited)}: {url}', flush=True)
        try:
            r = session.get(url, timeout=30, verify=False, allow_redirects=True)
            r.raise_for_status()
        except Exception as exc:
            failed.append({'url': url, 'error': repr(exc)})
            continue
        html = r.text
        if '<html' not in html[:1200].lower() and 'text/html' not in r.headers.get('Content-Type', '').lower():
            continue
        soup = BeautifulSoup(html, 'html.parser')
        page_url = r.url
        title = title_of(soup, page_url)
        text = soup.get_text('\n', strip=True)
        entity_type = classify_entity(page_url, title, text)
        category = classify_category(f'{page_url} {title} {text[:3000]}')
        desc = description_of(soup, text)
        points = selling_points(soup)

        for a in soup.find_all('a', href=True):
            link = normalize(a.get('href'), page_url)
            if not link:
                continue
            h = urlparse(link).netloc.lower().split(':')[0]
            if h in allowed and not is_asset(link) and link not in visited:
                queue.append(link)

        if not entity_type:
            ignored.append({'url': page_url, 'title': title, 'reason': '不属于产品/案例/解决方案/营销资料/证言视频'})
            continue

        base_dir = out / ENTRY_DIRS[entity_type] / category / slug(title)
        base_dir.mkdir(parents=True, exist_ok=True)
        image_dir = base_dir / 'images'
        download_dir = base_dir / 'downloads'
        image_dir.mkdir(exist_ok=True)
        download_dir.mkdir(exist_ok=True)

        entity_id = f'official-zh-CN-{entity_type}-{digest(page_url)}'
        metadata = {
            'id': entity_id,
            'entityType': entity_type,
            'title': title,
            'category': category,
            'sourceUrl': page_url,
            'sourceHash': hashlib.sha256(html.encode('utf-8', errors='ignore')).hexdigest(),
            'description': desc,
            'sellingPoints': points,
            'folder': str(base_dir.relative_to(out)),
            'crawledAt': now(),
            'imageCount': 0,
            'downloadCount': 0,
        }

        write_text(base_dir / '01_描述.txt', desc)
        if points:
            point_text = '\n\n'.join([f"## {p['title']}\n{p['description']}" for p in points])
        else:
            point_text = '未在页面中识别到明确的卖点标题与说明。'
        write_text(base_dir / '02_卖点标题与说明.txt', point_text)
        write_text(base_dir / '03_分类与来源.txt', f'分类：{category}\n类型：{entity_type}\n来源：{page_url}\n抓取时间：{now()}\n')
        write_text(base_dir / '04_页面正文摘录.txt', clean(text[:5000]))

        local_assets = []
        for asset_url, label in asset_links(page_url, soup):
            k = asset_type(asset_url)
            if k == 'image' and not download_images:
                continue
            if k == 'document' and not download_docs:
                continue
            if k == 'video' and not download_videos:
                continue
            saved = download_asset(asset_url, label, page_url, base_dir, max_mb=max_mb)
            if not saved:
                continue
            if saved.get('assetType') == 'failed':
                failed.append(saved)
                continue
            # 移动到规范子目录已经由 download_asset 完成
            local_assets.append(saved)
            assets.append(saved)
        metadata['imageCount'] = len([a for a in local_assets if a.get('assetType') == 'image'])
        metadata['downloadCount'] = len([a for a in local_assets if a.get('assetType') in {'document', 'video'}])
        metadata['assets'] = local_assets
        write_json(base_dir / 'metadata.json', metadata)
        write_text(base_dir / '05_图片清单.txt', '\n'.join([f"{a.get('fileName')}\n来源：{a.get('sourceUrl')}\n" for a in local_assets if a.get('assetType') == 'image']) or '未下载到图片。')
        entities.append(metadata)

    by_type = {}
    by_cat = {}
    for item in entities:
        by_type[item['entityType']] = by_type.get(item['entityType'], 0) + 1
        by_cat[item['category']] = by_cat.get(item['category'], 0) + 1

    write_json(out / '00_爬取报告' / '爬取摘要.json', {'generatedAt': now(), 'entityCount': len(entities), 'assetCount': len(assets), 'failedCount': len(failed), 'ignoredCount': len(ignored), 'byType': by_type, 'byCategory': by_cat})
    write_json(out / '00_爬取报告' / '失败资源.json', failed)
    write_json(out / '00_爬取报告' / '未入库页面索引.json', ignored)
    write_json(out / '06_文件资产索引' / '文件资产索引.json', assets)
    write_json(out / '10_upload_ready_entities.json', entities)

    report_lines = ['# AOTO 中文官网结构化资料爬取报告', '', f'生成时间：{now()}', '', f'- 结构化资料：{len(entities)}', f'- 资源文件：{len(assets)}', f'- 失败资源/页面：{len(failed)}', f'- 未入库页面：{len(ignored)}', '', '## 按类型统计']
    for k, v in by_type.items():
        report_lines.append(f'- {k}: {v}')
    report_lines.extend(['', '## 按分类统计'])
    for k, v in by_cat.items():
        report_lines.append(f'- {k}: {v}')
    write_text(out / '00_爬取报告' / '爬取摘要.txt', '\n'.join(report_lines))
    print(f'完成：{out}', flush=True)


if __name__ == '__main__':
    main()
