from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import scrapy
from bs4 import BeautifulSoup

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.svg', '.avif', '.bmp'}
DOC_EXTS = {'.pdf', '.ppt', '.pptx', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.rar', '.7z'}
VIDEO_EXTS = {'.mp4', '.mov', '.webm', '.m4v'}
CN_RE = re.compile(r'[\u4e00-\u9fff]')
PRODUCT_RE = re.compile(r'产品|product|series|显示屏|小间距|租赁|控制系统|led', re.I)
CASE_RE = re.compile(r'案例|项目|case|project|客户|机场|体育|场馆|studio|airport', re.I)
SOLUTION_RE = re.compile(r'解决方案|solution|market|行业|场景|XR|虚拟|体育|交通|零售|控制室|广电|会议', re.I)
MATERIAL_RE = re.compile(r'资料|下载|download|brochure|whitepaper|白皮书|彩页|画册|公司介绍|VI|营销|展会', re.I)
TESTIMONIAL_RE = re.compile(r'客户证言|客户采访|testimonial|interview|customer story|视频|video', re.I)
NEWS_RE = re.compile(r'新闻|news|event|marketing events|tech news', re.I)
ABOUT_RE = re.compile(r'关于|about|公司|荣誉|资质|esg|contact|联系', re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean(value: str) -> str:
    return re.sub(r'\s+', ' ', value or '').strip()


def safe_slug(value: str, fallback: str = 'untitled') -> str:
    value = clean(value)
    value = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff]+', '-', value).strip('-').lower()
    return value[:90] or fallback


def sha(text: str, length: int = 12) -> str:
    return hashlib.sha256((text or '').encode('utf-8', errors='ignore')).hexdigest()[:length]


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b'').hexdigest()


def normalize_url(url: str, base: str = '') -> str:
    if not url:
        return ''
    url = url.strip()
    if url.startswith(('data:', 'mailto:', 'tel:', 'javascript:')):
        return ''
    absolute = urljoin(base, url)
    absolute, _frag = urldefrag(absolute)
    return absolute


def ext_of(url: str) -> str:
    path = urlparse(url).path.lower()
    return '.' + path.rsplit('.', 1)[-1] if '.' in path else ''


def asset_type(url: str, content_type: str = '') -> str:
    ext = ext_of(url)
    ct = (content_type or '').lower()
    if ext in IMAGE_EXTS or ct.startswith('image/'):
        return 'image'
    if ext in DOC_EXTS or any(k in ct for k in ['pdf', 'powerpoint', 'presentation', 'zip', 'msword', 'excel']):
        return 'document'
    if ext in VIDEO_EXTS or ct.startswith('video/'):
        return 'video'
    return 'link'


class AotoCnSpider(scrapy.Spider):
    name = 'aoto_cn_full'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        seed_env = kwargs.get('seeds') or os.getenv('AOTO_SEEDS') or 'https://www.aoto.com/,https://www.aoto.com/cn/,https://www.aoto.com/zh/,https://cn.aoto.com/,https://www.aoto.com.cn/'
        self.start_urls = [u.strip() for u in seed_env.split(',') if u.strip()]
        domain_env = kwargs.get('allowed_domains') or os.getenv('AOTO_ALLOWED_DOMAINS') or 'www.aoto.com,aoto.com,cn.aoto.com,www.aoto.com.cn,aoto.com.cn,en.aoto.com'
        self.allowed_domains = [d.strip().lower() for d in domain_env.split(',') if d.strip()]
        self.output_root = Path(kwargs.get('out') or os.getenv('AOTO_CRAWL_OUT') or 'aoto-official-cn-full').resolve()
        self.max_pages = int(kwargs.get('max_pages') or os.getenv('AOTO_MAX_PAGES') or '0')
        self.max_file_mb = int(kwargs.get('max_file_mb') or os.getenv('AOTO_MAX_FILE_MB') or '80')
        self.download_images = str(kwargs.get('download_images') or os.getenv('AOTO_DOWNLOAD_IMAGES') or '1') not in {'0', 'false', 'False'}
        self.download_docs = str(kwargs.get('download_docs') or os.getenv('AOTO_DOWNLOAD_DOCS') or '1') not in {'0', 'false', 'False'}
        self.download_videos = str(kwargs.get('download_videos') or os.getenv('AOTO_DOWNLOAD_VIDEOS') or '0') in {'1', 'true', 'True'}
        self.preserve_url_structure = str(kwargs.get('preserve_url_structure') or os.getenv('AOTO_PRESERVE_URL_STRUCTURE') or '1') not in {'0', 'false', 'False'}
        self.pages = []
        self.assets = []
        self.failed = []
        self.skipped = []
        self.seen_asset_urls = set()
        for d in ['00_reports', '01_products', '02_cases', '03_solutions', '04_marketing_materials', '05_customer_testimonials', '06_news', '07_about', '08_file_assets', '09_relations', '10_upload_ready', '11_official_site_tree']:
            (self.output_root / d).mkdir(parents=True, exist_ok=True)

    def parse(self, response):
        if self.max_pages and len(self.pages) >= self.max_pages:
            return
        if not response.text:
            return
        url = response.url
        soup = BeautifulSoup(response.text, 'lxml')
        title = self.extract_title(soup, url)
        body_text = soup.get_text('\n', strip=True)
        module = self.classify_page(url, title, body_text)
        locale = self.detect_locale(url, body_text)
        page_id = f'official-{locale}-{module}-{sha(url)}'
        folder = self.page_folder(module, title, page_id)
        folder.mkdir(parents=True, exist_ok=True)

        meta = {
            'id': page_id,
            'url': url,
            'title': title,
            'module': module,
            'locale': locale,
            'sourceHash': hashlib.sha256(response.text.encode('utf-8', errors='ignore')).hexdigest(),
            'textLength': len(body_text),
            'crawledAt': now_iso(),
            'folder': str(folder.relative_to(self.output_root)),
            'summary': clean(body_text[:500]),
            'links': [],
            'images': [],
            'downloads': [],
            'sellingPoints': self.extract_selling_points(soup),
        }
        self.write_text(folder / 'source.html', response.text)
        self.write_text(folder / 'page-text.txt', body_text)

        for asset_url, label, source_kind in self.extract_asset_urls(response, soup):
            kind = asset_type(asset_url)
            asset = {
                'id': f'asset-{sha(asset_url, 16)}',
                'sourceUrl': asset_url,
                'pageUrl': url,
                'label': clean(label) or Path(urlparse(asset_url).path).name or asset_url,
                'module': module,
                'pageTitle': title,
                'assetType': kind,
                'sourceKind': source_kind,
                'localPath': '',
            }
            if kind == 'image':
                meta['images'].append(asset)
            elif kind in {'document', 'video'}:
                meta['downloads'].append(asset)
            should_download = (
                (kind == 'image' and self.download_images) or
                (kind == 'document' and self.download_docs) or
                (kind == 'video' and self.download_videos)
            )
            if should_download and asset_url not in self.seen_asset_urls:
                self.seen_asset_urls.add(asset_url)
                yield scrapy.Request(asset_url, callback=self.save_asset, cb_kwargs={'asset': asset}, errback=self.asset_failed, dont_filter=True)

        for a in soup.find_all('a', href=True):
            link = normalize_url(a.get('href'), url)
            if not link:
                continue
            label = clean(a.get_text(' ', strip=True))
            meta['links'].append({'label': label, 'url': link})
            if self.should_follow(link):
                yield scrapy.Request(link, callback=self.parse, errback=self.page_failed)

        meta['imageCount'] = len(meta['images'])
        meta['downloadCount'] = len(meta['downloads'])
        self.write_json(folder / 'metadata.json', meta)
        if self.preserve_url_structure:
            tree_folder = self.url_tree_folder(url)
            tree_folder.mkdir(parents=True, exist_ok=True)
            meta['officialTreeFolder'] = str(tree_folder.relative_to(self.output_root))
            self.write_text(tree_folder / 'source.html', response.text)
            self.write_text(tree_folder / 'page-text.txt', body_text)
            self.write_json(tree_folder / 'metadata.json', meta)
        self.pages.append(meta)
        yield {'type': 'page', **meta}

    def should_follow(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'}:
            return False
        host = parsed.netloc.lower().split(':')[0]
        if host not in self.allowed_domains:
            return False
        ext = ext_of(url)
        if ext in IMAGE_EXTS or ext in DOC_EXTS or ext in VIDEO_EXTS:
            return False
        if re.search(r'/wp-admin|/login|/cart|/checkout|\?s=|/feed/', url, re.I):
            return False
        return True

    def extract_title(self, soup: BeautifulSoup, url: str) -> str:
        values = []
        for sel in ['h1', "meta[property='og:title']", "meta[name='twitter:title']", 'title']:
            tag = soup.select_one(sel)
            if not tag:
                continue
            value = tag.get('content', '') if tag.name == 'meta' else tag.get_text(' ', strip=True)
            value = clean(value).replace(' - AOTO', '').replace(' – AOTO', '')
            if value:
                values.append(value)
        return values[0] if values else urlparse(url).path.strip('/') or url

    def detect_locale(self, url: str, text: str) -> str:
        if re.search(r'/zh|/cn|\.com\.cn|cn\.aoto', url, re.I):
            return 'zh-CN'
        return 'zh-CN' if len(CN_RE.findall(text[:5000])) >= 30 else 'en-US'

    def classify_page(self, url: str, title: str, text: str) -> str:
        bag = f'{url} {title} {text[:1600]}'
        if TESTIMONIAL_RE.search(bag):
            return 'customer-testimonials'
        if PRODUCT_RE.search(bag) and re.search(r'/product|产品|series', bag, re.I):
            return 'products'
        if CASE_RE.search(bag) and re.search(r'/case|案例|项目', bag, re.I):
            return 'cases'
        if SOLUTION_RE.search(bag) and re.search(r'/solution|/market|解决方案|行业|场景', bag, re.I):
            return 'solutions'
        if MATERIAL_RE.search(bag):
            return 'marketing-materials'
        if NEWS_RE.search(bag):
            return 'news'
        if ABOUT_RE.search(bag):
            return 'about'
        return 'general'

    def page_folder(self, module: str, title: str, page_id: str) -> Path:
        mapping = {
            'products': '01_products',
            'cases': '02_cases',
            'solutions': '03_solutions',
            'marketing-materials': '04_marketing_materials',
            'customer-testimonials': '05_customer_testimonials',
            'news': '06_news',
            'about': '07_about',
            'general': '07_about',
        }
        return self.output_root / mapping.get(module, '07_about') / f'{safe_slug(title)}-{page_id[-8:]}'

    def url_tree_folder(self, url: str) -> Path:
        parsed = urlparse(url)
        host = safe_slug(parsed.netloc or 'unknown-host')
        path = parsed.path.strip('/')
        if not path:
            return self.output_root / '11_official_site_tree' / host / '__home'
        parts = [safe_slug(part, 'index') for part in path.split('/') if part]
        if parsed.path.endswith('/'):
            return self.output_root / '11_official_site_tree' / host / Path(*parts) / '__index'
        return self.output_root / '11_official_site_tree' / host / Path(*parts)

    def extract_asset_urls(self, response, soup: BeautifulSoup):
        urls = []
        base = response.url
        for img in soup.find_all('img'):
            for attr in ['src', 'data-src', 'data-original', 'data-lazy-src']:
                u = normalize_url(img.get(attr, ''), base)
                if u:
                    urls.append((u, img.get('alt', ''), 'img'))
            srcset = img.get('srcset') or img.get('data-srcset')
            if srcset:
                for part in srcset.split(','):
                    u = normalize_url(part.strip().split(' ')[0], base)
                    if u:
                        urls.append((u, img.get('alt', ''), 'srcset'))
        for sel in ["meta[property='og:image']", "meta[name='twitter:image']"]:
            tag = soup.select_one(sel)
            if tag and tag.get('content'):
                urls.append((normalize_url(tag.get('content'), base), 'page cover', 'meta'))
        for a in soup.find_all('a', href=True):
            u = normalize_url(a.get('href'), base)
            if not u:
                continue
            ext = ext_of(u)
            label = clean(a.get_text(' ', strip=True))
            if ext in IMAGE_EXTS or ext in DOC_EXTS or ext in VIDEO_EXTS:
                urls.append((u, label, 'a'))
        for tag in soup.find_all(style=True):
            for match in re.finditer(r"url\(['\"]?([^)'\"\s]+)['\"]?\)", tag.get('style', ''), re.I):
                u = normalize_url(match.group(1), base)
                if u:
                    urls.append((u, 'background image', 'style'))
        seen = set()
        result = []
        for u, label, kind in urls:
            if u and u not in seen:
                seen.add(u)
                result.append((u, label, kind))
        return result

    def extract_selling_points(self, soup: BeautifulSoup):
        points = []
        for heading in soup.find_all(['h2', 'h3', 'h4']):
            title = clean(heading.get_text(' ', strip=True))
            if not title or len(title) > 90:
                continue
            body_parts = []
            for sib in heading.find_all_next():
                if sib.name in ['h2', 'h3', 'h4']:
                    break
                if sib.name in ['p', 'li']:
                    tx = clean(sib.get_text(' ', strip=True))
                    if tx:
                        body_parts.append(tx)
                if len(' '.join(body_parts)) > 450:
                    break
            body = clean(' '.join(body_parts))
            if body and 20 <= len(body) <= 600:
                if re.search(r'Hz|gamut|contrast|protection|refresh|scan|亮度|刷新|色域|对比|防护|维护|安装|节能|像素', f'{title} {body}', re.I):
                    points.append({'title': title, 'copy': body[:600]})
        return points[:24]

    def save_asset(self, response, asset):
        body = response.body or b''
        if not body:
            return
        size = len(body)
        if size > self.max_file_mb * 1024 * 1024:
            self.skipped.append({**asset, 'reason': f'file too large: {size}'})
            return
        content_type = response.headers.get('Content-Type', b'').decode('utf-8', 'ignore').split(';')[0]
        kind = asset_type(response.url, content_type)
        ext = ext_of(response.url)
        if not ext:
            ext = '.jpg' if kind == 'image' else '.mp4' if kind == 'video' else '.bin'
        module = asset.get('module') or 'general'
        file_name = f"{safe_slug(asset.get('label') or Path(urlparse(response.url).path).stem or kind)}-{sha(response.url, 8)}{ext}"
        subdir = 'images' if kind == 'image' else 'downloads' if kind == 'document' else 'videos' if kind == 'video' else 'links'
        folder = self.output_root / '08_file_assets' / module / subdir
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / file_name
        path.write_bytes(body)
        tree_local_path = ''
        if self.preserve_url_structure:
            tree_asset_folder = self.url_tree_folder(asset.get('pageUrl') or response.url) / '_assets' / subdir
            tree_asset_folder.mkdir(parents=True, exist_ok=True)
            tree_path = tree_asset_folder / file_name
            tree_path.write_bytes(body)
            tree_local_path = str(tree_path.relative_to(self.output_root))
        rec = {
            **asset,
            'sourceUrl': response.url,
            'assetType': kind,
            'mimeType': content_type,
            'fileName': file_name,
            'localPath': str(path.relative_to(self.output_root)),
            'officialTreeLocalPath': tree_local_path,
            'sizeBytes': size,
            'sha256': sha_bytes(body),
            'downloadedAt': now_iso(),
        }
        self.assets.append(rec)
        yield {'type': 'asset', **rec}

    def asset_failed(self, failure):
        self.failed.append({'kind': 'asset', 'url': failure.request.url, 'error': repr(failure.value)})

    def page_failed(self, failure):
        self.failed.append({'kind': 'page', 'url': failure.request.url, 'error': repr(failure.value)})

    def write_text(self, path: Path, value: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value or '', encoding='utf-8', errors='ignore')

    def write_json(self, path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    def closed(self, reason):
        pages_by_module = {}
        for p in self.pages:
            pages_by_module.setdefault(p['module'], []).append(p)
        assets_by_type = {}
        for a in self.assets:
            assets_by_type.setdefault(a['assetType'], []).append(a)
        self.write_json(self.output_root / '00_reports' / 'crawl-summary.json', {
            'generatedAt': now_iso(),
            'reason': reason,
            'seeds': self.start_urls,
            'allowedDomains': self.allowed_domains,
            'pageCount': len(self.pages),
            'assetCount': len(self.assets),
            'failedCount': len(self.failed),
            'skippedCount': len(self.skipped),
            'pagesByModule': {k: len(v) for k, v in pages_by_module.items()},
            'assetsByType': {k: len(v) for k, v in assets_by_type.items()},
        })
        self.write_json(self.output_root / '00_reports' / 'failed-pages.json', self.failed)
        self.write_json(self.output_root / '00_reports' / 'skipped-links.json', self.skipped)
        self.write_json(self.output_root / '08_file_assets' / 'images-index.json', assets_by_type.get('image', []))
        self.write_json(self.output_root / '08_file_assets' / 'downloads-index.json', assets_by_type.get('document', []))
        self.write_json(self.output_root / '08_file_assets' / 'videos-index.json', assets_by_type.get('video', []))
        self.write_json(self.output_root / '10_upload_ready' / 'products-for-import.json', self.as_import_records(pages_by_module.get('products', []), 'product'))
        self.write_json(self.output_root / '10_upload_ready' / 'cases-for-import.json', self.as_import_records(pages_by_module.get('cases', []), 'case'))
        self.write_json(self.output_root / '10_upload_ready' / 'solutions-for-import.json', self.as_import_records(pages_by_module.get('solutions', []), 'solution'))
        self.write_json(self.output_root / '10_upload_ready' / 'materials-for-import.json', self.as_import_records(pages_by_module.get('marketing-materials', []), 'material'))
        self.write_json(self.output_root / '10_upload_ready' / 'file-assets-for-import.json', self.assets)
        self.write_json(self.output_root / '09_relations' / 'relation-candidates.json', self.build_relation_candidates())
        lines = [
            '# AOTO 中文官网全量爬取报告', '', f'生成时间：{now_iso()}', f'结束原因：{reason}', '',
            '## 数量', '', f'- 页面：{len(self.pages)}', f'- 资源文件：{len(self.assets)}', f'- 失败：{len(self.failed)}', f'- 跳过：{len(self.skipped)}', '',
            '## 页面模块',
        ]
        for k, v in sorted(pages_by_module.items()):
            lines.append(f'- {k}: {len(v)}')
        lines.extend(['', '## 资源类型'])
        for k, v in sorted(assets_by_type.items()):
            lines.append(f'- {k}: {len(v)}')
        lines.extend(['', '## 说明', '', '- 本次只抓公开网页。', '- 本次不写数据库。', '- Relation 仅生成候选，需要人工确认。', '- 文件本体存放在 `08_file_assets`。'])
        (self.output_root / '00_reports' / 'crawl-summary.md').write_text('\n'.join(lines), encoding='utf-8')
        (self.output_root / '00_reports' / 'upload-plan.md').write_text(self.upload_plan_text(), encoding='utf-8')

    def as_import_records(self, pages, entity_type):
        return [{
            'id': p['id'],
            'entityType': entity_type,
            'title': p['title'],
            'locale': p['locale'],
            'sourceUrl': p['url'],
            'sourceHash': p.get('sourceHash', ''),
            'description': p.get('summary', ''),
            'coverUrl': self.first_image_for_page(p['url']),
            'folder': p['folder'],
            'sellingPoints': p.get('sellingPoints', []),
            'publishStatus': 'draft',
            'reviewStatus': 'manual_review',
        } for p in pages]

    def first_image_for_page(self, page_url: str) -> str:
        for a in self.assets:
            if a.get('pageUrl') == page_url and a.get('assetType') == 'image':
                return a.get('localPath', '')
        return ''

    def build_relation_candidates(self):
        pages = self.pages
        products = [p for p in pages if p['module'] == 'products']
        cases = [p for p in pages if p['module'] == 'cases']
        solutions = [p for p in pages if p['module'] == 'solutions']
        materials = [p for p in pages if p['module'] == 'marketing-materials']
        rels = []
        def add(src, tgt, rel_type, confidence, reason):
            rels.append({
                'id': f"rel-{sha(src['id'] + tgt['id'] + rel_type, 16)}",
                'sourceType': src['module'].rstrip('s'),
                'sourceId': src['id'],
                'sourceTitle': src['title'],
                'targetType': tgt['module'].rstrip('s'),
                'targetId': tgt['id'],
                'targetTitle': tgt['title'],
                'relationType': rel_type,
                'confidence': confidence,
                'reason': reason,
            })
        for c in cases:
            bag = f"{c['title']} {c.get('summary','')}".lower()
            for p in products:
                if p['title'].lower() and p['title'].lower() in bag:
                    add(c, p, 'related_product', 'medium', '案例标题/摘要命中产品名称')
        for s in solutions:
            bag = f"{s['title']} {s.get('summary','')}".lower()
            for p in products:
                if p['title'].lower() and p['title'].lower() in bag:
                    add(s, p, 'recommended_product', 'medium', '解决方案标题/摘要命中产品名称')
            for c in cases:
                words = set(re.findall(r'[A-Za-z0-9\u4e00-\u9fff]{3,}', bag))
                cwords = set(re.findall(r'[A-Za-z0-9\u4e00-\u9fff]{3,}', f"{c['title']} {c.get('summary','')}".lower()))
                shared = words & cwords
                if len(shared) >= 2:
                    add(s, c, 'related_case', 'low', '解决方案与案例关键词重合：' + '、'.join(list(shared)[:5]))
        for m in materials:
            bag = m['title'].lower()
            for p in products:
                if p['title'].lower() and p['title'].lower() in bag:
                    add(p, m, 'related_material', 'high', '资料名称命中产品名称')
        return rels

    def upload_plan_text(self):
        return '''# 上传计划

## 建议顺序

1. 先人工检查 `00_reports/crawl-summary.md`。
2. 再检查 `10_upload_ready` 下的 JSON。
3. 先导入 Product / Case / Solution 主数据草稿。
4. 再导入 FileAsset。
5. 再导入 Material。
6. 最后人工确认 Relation 候选。

## 禁止

- 不要直接覆盖现有中文站数据。
- 不要自动发布所有抓取内容。
- 不要把图片/PDF/PPT/视频写入 PostgreSQL。
- 不要把 low confidence Relation 自动入库。
'''
