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
CN_RE = re.compile(r'[\u4e00-\u9fff]')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def slug(text: str, fallback: str = 'untitled') -> str:
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


def kind_of(url: str, content_type: str = '') -> str:
    e = ext(url)
    ct = content_type.lower()
    if e in IMAGE_EXTS or ct.startswith('image/'):
        return 'image'
    if e in DOC_EXTS or any(x in ct for x in ['pdf', 'powerpoint', 'zip', 'msword', 'excel']):
        return 'document'
    if e in VIDEO_EXTS or ct.startswith('video/'):
        return 'video'
    return 'link'


class CrawlCN:
    def __init__(self):
        self.seeds = [u.strip() for u in os.getenv('AOTO_SEEDS', 'https://www.aoto.com/').split(',') if u.strip()]
        self.allowed = {d.strip().lower() for d in os.getenv('AOTO_ALLOWED_DOMAINS', 'www.aoto.com,aoto.com').split(',') if d.strip()}
        self.out = Path(os.getenv('AOTO_CRAWL_OUT', 'aoto-official-cn-full')).resolve()
        self.max_pages = int(os.getenv('AOTO_MAX_PAGES', '0') or '0')
        self.max_mb = int(os.getenv('AOTO_MAX_FILE_MB', '80') or '80')
        self.download_images = os.getenv('AOTO_DOWNLOAD_IMAGES', '1') != '0'
        self.download_docs = os.getenv('AOTO_DOWNLOAD_DOCS', '1') != '0'
        self.download_videos = os.getenv('AOTO_DOWNLOAD_VIDEOS', '0') == '1'
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 AOTO-CN-Crawler/1.2', 'Accept-Language': 'zh-CN,zh;q=0.9'})
        self.visited = set()
        self.assets_seen = set()
        self.pages = []
        self.assets = []
        self.failed = []
        self.skipped = []
        for name in ['00_reports', '01_products', '02_cases', '03_solutions', '04_marketing_materials', '05_customer_testimonials', '06_news', '07_about', '08_file_assets', '09_relations', '10_upload_ready', '11_official_site_tree']:
            (self.out / name).mkdir(parents=True, exist_ok=True)

    def allowed_url(self, url: str) -> bool:
        p = urlparse(url)
        return p.scheme in {'http', 'https'} and p.netloc.lower().split(':')[0] in self.allowed

    def should_follow(self, url: str) -> bool:
        if not self.allowed_url(url):
            return False
        if ext(url) in IMAGE_EXTS | DOC_EXTS | VIDEO_EXTS:
            return False
        return not re.search(r'/wp-admin|/login|/cart|/checkout|/feed/|\?s=', url, re.I)

    def get(self, url: str):
        try:
            r = self.session.get(url, timeout=30, verify=False, allow_redirects=True)
            r.raise_for_status()
            return r
        except Exception as e:
            self.failed.append({'kind': 'page', 'url': url, 'error': repr(e)})
            return None

    def run(self):
        queue = deque(self.seeds)
        while queue:
            url = normalize(queue.popleft())
            if not url or url in self.visited:
                continue
            if self.max_pages and len(self.pages) >= self.max_pages:
                break
            if not self.allowed_url(url):
                self.skipped.append({'url': url, 'reason': '域名不在允许范围'})
                continue
            self.visited.add(url)
            print(f'[爬取] {len(self.pages)+1}: {url}')
            res = self.get(url)
            if not res:
                continue
            html = res.text
            if '<html' not in html[:1000].lower() and 'text/html' not in res.headers.get('Content-Type', '').lower():
                self.skipped.append({'url': url, 'reason': '不是 HTML 页面'})
                continue
            page, links = self.process_page(res.url, html)
            self.pages.append(page)
            for link in links:
                if link not in self.visited and self.should_follow(link):
                    queue.append(link)
            time.sleep(0.2)
        self.write_reports()

    def process_page(self, url: str, html: str):
        soup = BeautifulSoup(html, 'html.parser')
        text = soup.get_text('\n', strip=True)
        title = self.title_of(soup, url)
        module = self.module_of(url, title, text)
        page_id = f'official-zh-CN-{module}-{digest(url)}'
        folder = self.business_folder(module, title, page_id)
        tree = self.tree_folder(url)
        folder.mkdir(parents=True, exist_ok=True)
        tree.mkdir(parents=True, exist_ok=True)
        page = {'id': page_id, 'url': url, 'title': title, 'module': module, 'locale': 'zh-CN', 'sourceHash': hashlib.sha256(html.encode('utf-8', errors='ignore')).hexdigest(), 'summary': clean(text[:500]), 'textLength': len(text), 'crawledAt': now(), 'folder': str(folder.relative_to(self.out)), 'officialTreeFolder': str(tree.relative_to(self.out)), 'links': [], 'images': [], 'downloads': [], 'sellingPoints': self.selling_points(soup)}
        self.write_text(folder / 'source.html', html)
        self.write_text(folder / 'page-text.txt', text)
        self.write_text(tree / 'source.html', html)
        self.write_text(tree / 'page-text.txt', text)
        links = []
        for a in soup.find_all('a', href=True):
            link = normalize(a.get('href'), url)
            if not link:
                continue
            label = clean(a.get_text(' ', strip=True))
            page['links'].append({'label': label, 'url': link})
            links.append(link)
        for asset_url, label in self.asset_links(url, soup):
            k = kind_of(asset_url)
            item = {'id': 'asset-' + digest(asset_url, 16), 'sourceUrl': asset_url, 'pageUrl': url, 'label': label or Path(urlparse(asset_url).path).name or asset_url, 'assetType': k, 'module': module, 'pageTitle': title, 'localPath': '', 'officialTreeLocalPath': ''}
            if k == 'image':
                page['images'].append(item)
            elif k in {'document', 'video'}:
                page['downloads'].append(item)
            if asset_url not in self.assets_seen and ((k == 'image' and self.download_images) or (k == 'document' and self.download_docs) or (k == 'video' and self.download_videos)):
                self.assets_seen.add(asset_url)
                saved = self.download_asset(item)
                if saved:
                    self.assets.append(saved)
        page['imageCount'] = len(page['images'])
        page['downloadCount'] = len(page['downloads'])
        self.write_json(folder / 'metadata.json', page)
        self.write_json(tree / 'metadata.json', page)
        return page, links

    def title_of(self, soup, url: str) -> str:
        for sel in ['h1', "meta[property='og:title']", 'title']:
            tag = soup.select_one(sel)
            if tag:
                value = tag.get('content') if tag.name == 'meta' else tag.get_text(' ', strip=True)
                if clean(value):
                    return clean(value).replace(' - AOTO', '')
        return urlparse(url).path.strip('/') or url

    def module_of(self, url: str, title: str, text: str) -> str:
        bag = f'{url} {title} {text[:1600]}'
        if re.search(r'客户证言|客户采访|视频|testimonial|interview', bag, re.I):
            return 'customer-testimonials'
        if re.search(r'/product|产品|显示屏|小间距|LED', bag, re.I):
            return 'products'
        if re.search(r'/case|案例|项目|客户', bag, re.I):
            return 'cases'
        if re.search(r'/solution|/market|解决方案|行业|场景|控制室|交通|体育|零售|XR|虚拟', bag, re.I):
            return 'solutions'
        if re.search(r'资料|下载|白皮书|彩页|画册|公司介绍|VI|营销|展会', bag, re.I):
            return 'marketing-materials'
        if re.search(r'新闻|动态|news|event', bag, re.I):
            return 'news'
        return 'about'

    def business_folder(self, module, title, page_id):
        mapping = {'products': '01_products', 'cases': '02_cases', 'solutions': '03_solutions', 'marketing-materials': '04_marketing_materials', 'customer-testimonials': '05_customer_testimonials', 'news': '06_news', 'about': '07_about'}
        return self.out / mapping.get(module, '07_about') / f'{slug(title)}-{page_id[-8:]}'

    def tree_folder(self, url: str):
        p = urlparse(url)
        host = slug(p.netloc or 'unknown')
        parts = [slug(x, 'index') for x in p.path.strip('/').split('/') if x]
        if not parts:
            return self.out / '11_official_site_tree' / host / '__home'
        return self.out / '11_official_site_tree' / host / Path(*parts) / ('__index' if p.path.endswith('/') else '')

    def asset_links(self, base, soup):
        out = []
        for img in soup.find_all('img'):
            for attr in ['src', 'data-src', 'data-original', 'data-lazy-src']:
                u = normalize(img.get(attr, ''), base)
                if u:
                    out.append((u, clean(img.get('alt', ''))))
            srcset = img.get('srcset') or img.get('data-srcset')
            if srcset:
                for part in srcset.split(','):
                    u = normalize(part.strip().split(' ')[0], base)
                    if u:
                        out.append((u, clean(img.get('alt', ''))))
        for sel in ["meta[property='og:image']", "meta[name='twitter:image']"]:
            tag = soup.select_one(sel)
            if tag and tag.get('content'):
                out.append((normalize(tag.get('content'), base), '页面封面'))
        for a in soup.find_all('a', href=True):
            u = normalize(a.get('href'), base)
            if ext(u) in IMAGE_EXTS | DOC_EXTS | VIDEO_EXTS:
                out.append((u, clean(a.get_text(' ', strip=True))))
        seen = set()
        result = []
        for u, label in out:
            if u and u not in seen:
                seen.add(u)
                result.append((u, label))
        return result

    def download_asset(self, item):
        try:
            r = self.session.get(item['sourceUrl'], timeout=45, verify=False, allow_redirects=True)
            r.raise_for_status()
            body = r.content or b''
            if len(body) > self.max_mb * 1024 * 1024:
                self.skipped.append({**item, 'reason': '文件过大'})
                return None
            k = kind_of(r.url, r.headers.get('Content-Type', ''))
            e = ext(r.url) or ('.jpg' if k == 'image' else '.mp4' if k == 'video' else '.bin')
            sub = 'images' if k == 'image' else 'downloads' if k == 'document' else 'videos'
            name = f"{slug(item.get('label') or Path(urlparse(r.url).path).stem or k)}-{digest(r.url, 8)}{e}"
            folder = self.out / '08_file_assets' / item['module'] / sub
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / name
            path.write_bytes(body)
            tree_path = ''
            tree_folder = self.tree_folder(item['pageUrl']) / '_assets' / sub
            tree_folder.mkdir(parents=True, exist_ok=True)
            tp = tree_folder / name
            tp.write_bytes(body)
            tree_path = str(tp.relative_to(self.out))
            return {**item, 'sourceUrl': r.url, 'assetType': k, 'mimeType': r.headers.get('Content-Type', '').split(';')[0], 'fileName': name, 'localPath': str(path.relative_to(self.out)), 'officialTreeLocalPath': tree_path, 'sizeBytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(), 'downloadedAt': now()}
        except Exception as e:
            self.failed.append({'kind': 'asset', 'url': item['sourceUrl'], 'error': repr(e)})
            return None

    def selling_points(self, soup):
        points = []
        for h in soup.find_all(['h2', 'h3', 'h4']):
            title = clean(h.get_text(' ', strip=True))
            if not title or len(title) > 80:
                continue
            parts = []
            for sib in h.find_all_next():
                if sib.name in ['h2', 'h3', 'h4']:
                    break
                if sib.name in ['p', 'li']:
                    tx = clean(sib.get_text(' ', strip=True))
                    if tx:
                        parts.append(tx)
                if len(' '.join(parts)) > 450:
                    break
            body = clean(' '.join(parts))
            if body and re.search(r'亮度|刷新|色域|对比|防护|维护|安装|节能|像素|Hz|gamut|contrast|protection|refresh', title + body, re.I):
                points.append({'title': title, 'copy': body[:600]})
        return points[:24]

    def write_reports(self):
        by_module = {}
        by_asset = {}
        for p in self.pages:
            by_module.setdefault(p['module'], []).append(p)
        for a in self.assets:
            by_asset.setdefault(a['assetType'], []).append(a)
        self.write_json(self.out / '00_reports' / 'crawl-summary.json', {'generatedAt': now(), 'seeds': self.seeds, 'allowedDomains': sorted(self.allowed), 'pageCount': len(self.pages), 'assetCount': len(self.assets), 'failedCount': len(self.failed), 'skippedCount': len(self.skipped), 'pagesByModule': {k: len(v) for k, v in by_module.items()}, 'assetsByType': {k: len(v) for k, v in by_asset.items()}})
        self.write_json(self.out / '00_reports' / 'failed-pages.json', self.failed)
        self.write_json(self.out / '00_reports' / 'skipped-links.json', self.skipped)
        self.write_json(self.out / '08_file_assets' / 'images-index.json', by_asset.get('image', []))
        self.write_json(self.out / '08_file_assets' / 'downloads-index.json', by_asset.get('document', []))
        self.write_json(self.out / '08_file_assets' / 'videos-index.json', by_asset.get('video', []))
        self.write_json(self.out / '10_upload_ready' / 'products-for-import.json', self.import_records(by_module.get('products', []), 'product'))
        self.write_json(self.out / '10_upload_ready' / 'cases-for-import.json', self.import_records(by_module.get('cases', []), 'case'))
        self.write_json(self.out / '10_upload_ready' / 'solutions-for-import.json', self.import_records(by_module.get('solutions', []), 'solution'))
        self.write_json(self.out / '10_upload_ready' / 'materials-for-import.json', self.import_records(by_module.get('marketing-materials', []), 'material'))
        self.write_json(self.out / '10_upload_ready' / 'file-assets-for-import.json', self.assets)
        md = ['# AOTO 中文官网全量爬取报告', '', f'生成时间：{now()}', '', f'- 页面：{len(self.pages)}', f'- 资源文件：{len(self.assets)}', f'- 失败：{len(self.failed)}', f'- 跳过：{len(self.skipped)}', '', '## 页面模块']
        for k, v in by_module.items():
            md.append(f'- {k}: {len(v)}')
        md.extend(['', '## 资源类型'])
        for k, v in by_asset.items():
            md.append(f'- {k}: {len(v)}')
        self.write_text(self.out / '00_reports' / 'crawl-summary.md', '\n'.join(md))

    def import_records(self, pages, entity_type):
        return [{'id': p['id'], 'entityType': entity_type, 'title': p['title'], 'locale': 'zh-CN', 'sourceUrl': p['url'], 'sourceHash': p['sourceHash'], 'description': p['summary'], 'coverUrl': self.first_image(p['url']), 'folder': p['folder'], 'sellingPoints': p.get('sellingPoints', []), 'publishStatus': 'draft', 'reviewStatus': 'manual_review'} for p in pages]

    def first_image(self, page_url):
        for a in self.assets:
            if a.get('pageUrl') == page_url and a.get('assetType') == 'image':
                return a.get('localPath', '')
        return ''

    def write_text(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value or '', encoding='utf-8', errors='ignore')

    def write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    CrawlCN().run()
