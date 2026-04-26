#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
官報「会社その他」セクションのHTML/PDFを取得する Playwright スクリプト。

既定ではトップページから最新の本紙目次をたどり、「会社その他」の開始ページから
当該号の最終ページまでを保存します。`--section-url` を指定すると、そのページを
開始ページとして同様に取得します。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Playwright, sync_playwright

KANPO_INDEX_URL = "https://www.kanpo.go.jp/index.html"
DEFAULT_SECTION_NAME = "会社その他"
TOC_PATH_RE = re.compile(r"/\d{8}/\d{8}h\d{5}/\d{8}h\d{5}0000f\.html$")
SECTION_PAGE_RE = re.compile(r"(?P<prefix>.+?)(?P<page>\d{3})f\.html$")
ISSUE_DATE_RE = re.compile(r"/(?P<date>\d{8})/")
ISSUE_PREFIX_RE = re.compile(r"/(?P<prefix>\d{8}h\d{5})/")
PDF_EMBED_RE = re.compile(r'<embed[^>]+src=["\'](?P<src>[^"\']+)["\']', re.IGNORECASE)


@dataclass
class PageDownload:
    page_number: int
    wrapper_url: str
    inner_url: str
    pdf_url: str
    wrapper_file: str
    inner_file: str
    pdf_file: str


@dataclass
class DownloadManifest:
    issue_date: str
    issue_prefix: str
    issue_label: str
    section_name: str
    toc_url: Optional[str]
    section_url: str
    start_page: int
    end_page: int
    total_pages: int
    downloaded_pages: list[PageDownload]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="官報の「会社その他」セクションを取得してローカル保存します。"
    )
    parser.add_argument(
        "--section-url",
        help="会社その他の開始ページURL。未指定時はトップページから最新号を探索します。",
    )
    parser.add_argument(
        "--section-name",
        default=DEFAULT_SECTION_NAME,
        help=f"目次から探すセクション名（既定: {DEFAULT_SECTION_NAME}）。",
    )
    parser.add_argument(
        "--out-dir",
        default="downloads",
        help="保存先の親ディレクトリ（既定: downloads）。",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="取得ページ数の上限。テスト用。",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="ブラウザを表示して実行します。",
    )
    args = parser.parse_args()
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages は 1 以上を指定してください。")
    return args


def ensure_ok(response, url: str) -> None:
    if response is None or not response.ok:
        status = "no response" if response is None else str(response.status)
        raise RuntimeError(f"取得に失敗しました: {url} (status={status})")


def ordered_unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item and item not in seen:
            ordered.append(item)
            seen.add(item)
    return ordered


def find_latest_toc_url(page: Page) -> str:
    response = page.goto(KANPO_INDEX_URL, wait_until="domcontentloaded")
    ensure_ok(response, KANPO_INDEX_URL)

    hrefs = page.locator("a").evaluate_all(
        "els => els.map(el => el.href).filter(Boolean)"
    )
    for href in ordered_unique(hrefs):
        if TOC_PATH_RE.search(urlparse(href).path):
            return href
    raise RuntimeError("最新の本紙目次URLをトップページから見つけられませんでした。")


def find_section_url_from_toc(page: Page, toc_url: str, section_name: str) -> str:
    response = page.goto(toc_url, wait_until="domcontentloaded")
    ensure_ok(response, toc_url)

    links = page.locator("a").evaluate_all(
        """
        els => els.map(el => ({
            href: el.href,
            text: (el.textContent || '').replace(/\\s+/g, ' ').trim()
        }))
        """
    )
    for link in links:
        if section_name in link["text"]:
            return link["href"]
    raise RuntimeError(f"目次に「{section_name}」へのリンクが見つかりませんでした。")


def get_issue_metadata(page: Page, section_url: str) -> dict[str, str | int]:
    response = page.goto(section_url, wait_until="domcontentloaded")
    ensure_ok(response, section_url)

    current_page = int(page.locator("#skipPage").input_value().strip())
    total_pages = int(page.locator("#pageAll").inner_text().strip())
    issue_label = page.locator("p.date").inner_text().strip()

    date_match = ISSUE_DATE_RE.search(section_url)
    prefix_match = ISSUE_PREFIX_RE.search(section_url)
    page_match = SECTION_PAGE_RE.search(section_url)
    if not date_match or not prefix_match or not page_match:
        raise RuntimeError(f"URL形式を解釈できませんでした: {section_url}")

    return {
        "issue_date": date_match.group("date"),
        "issue_prefix": prefix_match.group("prefix"),
        "issue_label": issue_label,
        "start_page": current_page,
        "total_pages": total_pages,
        "wrapper_prefix": page_match.group("prefix"),
        "page_width": len(page_match.group("page")),
    }


def save_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def save_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def download_section(
    playwright: Playwright,
    section_url: Optional[str],
    section_name: str,
    out_dir: Path,
    max_pages: Optional[int],
    headed: bool,
) -> Path:
    browser = playwright.chromium.launch(headless=not headed)
    context = browser.new_context()
    page = context.new_page()
    request_context = playwright.request.new_context()

    toc_url: Optional[str] = None
    try:
        if not section_url:
            toc_url = find_latest_toc_url(page)
            section_url = find_section_url_from_toc(page, toc_url, section_name)

        metadata = get_issue_metadata(page, section_url)
        start_page = int(metadata["start_page"])
        total_pages = int(metadata["total_pages"])
        end_page = (
            total_pages
            if max_pages is None
            else min(total_pages, start_page + max_pages - 1)
        )

        run_dir = out_dir / f"{metadata['issue_date']}_{metadata['issue_prefix']}_{slugify(section_name)}"
        wrappers_dir = run_dir / "wrappers"
        inner_dir = run_dir / "inner"
        pdf_dir = run_dir / "pdf"

        downloads: list[PageDownload] = []
        wrapper_prefix = str(metadata["wrapper_prefix"])
        page_width = int(metadata["page_width"])

        if toc_url:
            toc_response = request_context.get(toc_url)
            ensure_ok(toc_response, toc_url)
            save_text(run_dir / "toc.html", toc_response.text())

        for page_number in range(start_page, end_page + 1):
            wrapper_url = f"{wrapper_prefix}{page_number:0{page_width}d}f.html"
            wrapper_response = page.goto(wrapper_url, wait_until="domcontentloaded")
            ensure_ok(wrapper_response, wrapper_url)

            wrapper_file = wrappers_dir / Path(urlparse(wrapper_url).path).name
            save_text(wrapper_file, page.content())

            iframe_src = page.locator("iframe").get_attribute("src")
            if not iframe_src:
                raise RuntimeError(f"iframe が見つかりませんでした: {wrapper_url}")

            inner_url = urljoin(wrapper_url, iframe_src)
            inner_response = request_context.get(inner_url)
            ensure_ok(inner_response, inner_url)
            inner_html = inner_response.text()

            inner_file = inner_dir / Path(urlparse(inner_url).path).name
            save_text(inner_file, inner_html)

            pdf_match = PDF_EMBED_RE.search(inner_html)
            if not pdf_match:
                raise RuntimeError(f"PDFの embed 要素が見つかりませんでした: {inner_url}")

            pdf_url = urljoin(inner_url, pdf_match.group("src"))
            pdf_response = request_context.get(pdf_url)
            ensure_ok(pdf_response, pdf_url)

            pdf_file = pdf_dir / Path(urlparse(pdf_url).path).name
            save_bytes(pdf_file, pdf_response.body())

            downloads.append(
                PageDownload(
                    page_number=page_number,
                    wrapper_url=wrapper_url,
                    inner_url=inner_url,
                    pdf_url=pdf_url,
                    wrapper_file=str(wrapper_file.relative_to(run_dir)),
                    inner_file=str(inner_file.relative_to(run_dir)),
                    pdf_file=str(pdf_file.relative_to(run_dir)),
                )
            )

        manifest = DownloadManifest(
            issue_date=str(metadata["issue_date"]),
            issue_prefix=str(metadata["issue_prefix"]),
            issue_label=str(metadata["issue_label"]),
            section_name=section_name,
            toc_url=toc_url,
            section_url=section_url,
            start_page=start_page,
            end_page=end_page,
            total_pages=total_pages,
            downloaded_pages=downloads,
        )
        save_text(
            run_dir / "manifest.json",
            json.dumps(asdict(manifest), ensure_ascii=False, indent=2),
        )
        return run_dir
    finally:
        request_context.dispose()
        context.close()
        browser.close()


def slugify(text: str) -> str:
    if text == DEFAULT_SECTION_NAME:
        return "company-other"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug or "section"


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir).resolve()

    try:
        with sync_playwright() as playwright:
            run_dir = download_section(
                playwright=playwright,
                section_url=args.section_url,
                section_name=args.section_name,
                out_dir=out_dir,
                max_pages=args.max_pages,
                headed=args.headed,
            )
    except (RuntimeError, PlaywrightError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "Chromium が未導入なら `playwright install chromium` を実行してください。",
            file=sys.stderr,
        )
        return 1

    print(f"Saved to: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
