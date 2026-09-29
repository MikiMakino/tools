"""One daily PDF acquisition; retain originals before any document processing."""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from official_fetcher import FetchCancelled, FetchError, PDF_PATTERN, VIEWER_PATTERN, official_url


JST = timezone(timedelta(hours=9))


def _jst_now():
    return datetime.now(JST)


def _process_document(*args, **kwargs):
    # Keep download/storage usable even when document processing cannot start.
    from pipeline import process_document
    return process_document(*args, **kwargs)


def _display_status(status):
    return {'completed': '取得済み', 'partial': '一部取得',
            'failed': '取得失敗', 'cancelled': '取得失敗',
            'running': '取得失敗'}.get(status, '未取得')


def _validate_issues(issues, day, maximum):
    items, seen = [], set()
    for issue in issues:
        if getattr(issue, 'date', None) != day:
            raise ValueError('当日（日本時間）の官報だけをまとめて取得できます。')
        viewer = official_url(issue.viewer_url)
        match = VIEWER_PATTERN.fullmatch(urlsplit(viewer).path)
        if not match or match['day'] != day.replace('-', ''):
            raise ValueError('発行日と当日の公式PDF表示リンクが一致しません。')
        if viewer not in seen:
            seen.add(viewer)
            items.append(issue)
    if not items:
        raise ValueError('当日の取得対象がありません。公開一覧を確認してください。')
    if len(items) > maximum:
        raise ValueError('当日の取得対象が1回の上限を超えています。')
    return items


def find_saved_issue(store, issue):
    """Match a saved real PDF link to its advertised viewer without requesting it."""
    expected = VIEWER_PATTERN.fullmatch(urlsplit(issue.viewer_url).path).groupdict()
    for saved in store.documents():
        source = saved.get('source_url', '')
        try:
            official_url(source)
        except FetchError:
            continue
        match = PDF_PATTERN.fullmatch(urlsplit(source).path)
        if source != issue.viewer_url and (not match or match.groupdict() != expected):
            continue
        document = store.document(saved['id'])
        path = Path(document['path'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != document['id']:
            raise ValueError('取得済み原本がないか内容が変更されています。バックアップから復元してください。')
        return document
    return None


def run_daily_acquisition(store, fetcher, issues, ocr=None, layout_ocr=None,
                          cancelled=lambda: False, progress=lambda message: None):
    """Return acquisition status independently of later OCR/notice processing.

    The caller discovers and shows today's issues before calling this function.
    Once claimed, today's batch cannot be retried, including after cancellation.
    Successful archives count as downloaded even if processing needs attention.
    """
    now = _jst_now()
    day = now.date().isoformat()
    items = _validate_issues(issues, day, getattr(fetcher, 'max_items', 40))
    checked_at = now.isoformat(timespec='seconds')
    stamp = now.strftime('%Y-%m-%d %H:%M') + '（日本時間）に取得を開始した選択分'
    result = {'status': '未取得', 'message': '', 'day': day, 'checked_at': checked_at,
              'attempted': False, 'total': len(items), 'downloaded': 0, 'failed': 0,
              'reused': 0,
              'not_downloaded': len(items), 'processed': 0, 'documents': [],
              'processing_results': [], 'errors': [], 'retained_paths': []}
    if cancelled():
        result['message'] = '取得開始前に中止しました。'
        return result
    if not store.claim_download_day(day):
        previous = store.download_day(day) or {}
        result['status'] = _display_status(previous.get('status'))
        result['message'] = ('本日は既に取得を開始しています。再起動後も同日の再取得は行いません。'
                             '保存済み文書を確認し、未取得分は手動で対応してください。')
        result['previous_attempt'] = previous
        return result

    result['attempted'] = True
    batch = None
    stopped = False
    was_cancelled = False
    try:
        staging = store.folder / 'download_staging'
        staging.mkdir(parents=True, exist_ok=True)
        batch = Path(tempfile.mkdtemp(prefix=day + '-', dir=staging))
        for number, issue in enumerate(items, 1):
            if cancelled():
                was_cancelled = True
                break
            progress(f'PDF取得・原本保存 {number}/{len(items)}件: {issue.title}')
            try:
                saved = find_saved_issue(store, issue)
            except Exception as error:
                result['failed'] += 1
                result['errors'].append({'phase': 'archive', 'title': issue.title,
                                         'message': str(error)})
                stopped = True
                break
            if saved is not None:
                result['documents'].append(saved)
                result['downloaded'] += 1
                result['reused'] += 1
                continue
            try:
                received = fetcher.download(issue, batch)
            except FetchError as error:
                result['failed'] += 1
                result['errors'].append({'phase': 'download', 'title': issue.title,
                                         'message': str(error)})
                if isinstance(error, FetchCancelled):
                    was_cancelled = True
                if error.stop_all:
                    stopped = True
                    break
                continue
            try:
                # Persist first, even if cancellation arrived as download ended.
                document = store.archive_pdf(received.path, source_url=received.url)
            except Exception as error:
                result['failed'] += 1
                result['retained_paths'].append(str(received.path))
                result['errors'].append({'phase': 'archive', 'title': issue.title,
                                         'message': str(error)})
                stopped = True
                break
            result['documents'].append(document)
            result['downloaded'] += 1
            # Remove only this run's received file after archive_pdf succeeds.
            try:
                received_path = Path(received.path)
                received_path.resolve().relative_to(batch.resolve())
                received_path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
    except Exception as error:
        stopped = True
        result['errors'].append({'phase': 'download', 'message': str(error)})
        result['failed'] += 1
    finally:
        if batch is not None:
            # Failed archives remain available for manual recovery; no recursive deletion.
            try:
                batch.rmdir()
            except OSError:
                pass

    result['not_downloaded'] = len(items) - result['downloaded']
    if result['downloaded'] == len(items):
        storage_status, result['status'] = 'completed', '取得済み'
    elif result['downloaded']:
        storage_status, result['status'] = 'partial', '一部取得'
    else:
        storage_status, result['status'] = 'failed', '取得失敗'
    if was_cancelled and not result['downloaded']:
        storage_status = 'cancelled'
    detail = f"{stamp}。原本保存 {result['downloaded']}/{len(items)}件（{result['status']}）。"
    if result['reused']:
        detail += f"うち{result['reused']}件は内容を確認した保存済みPDFを使用しました。"
    if was_cancelled:
        detail += '取得を中止しました。'
    elif stopped:
        detail += '取得を停止しました。'
    if result['errors']:
        detail += ' ' + ' / '.join(error['message'] for error in result['errors'])
    if result['retained_paths']:
        detail += ' 原本登録に失敗した受信PDFはdownload_staging内に残しています。'
    if result['not_downloaded']:
        detail += ' 未取得分は手動で対応してください。同日の再取得は行いません。'
    # Download state is final before slow OCR starts or is interrupted.
    store.finish_download_day(storage_status, detail, day)
    result['message'] = detail

    for document in result['documents']:
        if was_cancelled or cancelled():
            break
        try:
            processed = _process_document(store, document, ocr=ocr, layout_ocr=layout_ocr,
                                          cancelled=cancelled, progress=progress)
        except Exception as error:
            processed = {'status': 'failed', 'document': document,
                         'message': '保存済みPDFの処理に失敗しました: ' + str(error),
                         'processed': 0, 'failed': 1}
            try:
                store.set_processing(document['id'], 'failed', processed['message'])
            except Exception as record_error:
                processed['message'] += ' 処理状態の保存にも失敗しました: ' + str(record_error)
        result['processing_results'].append(processed)
        result['processed'] += processed.get('processed', 0)
        if processed.get('status') == 'cancelled':
            break
    result['message'] += ' 公告の読み取り状況は文書ごとの状態を確認してください。'
    return result
