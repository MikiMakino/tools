"""Resumable document processing. Machine output always awaits human review."""
from pathlib import Path
import json


def process_document(store, document, ocr=None, layout_ocr=None, cancelled=None,
                     progress=None, force=False):
    """Process an already archived original, committing each page separately.

    A confirmed human scope is never replaced by a machine suggestion. Successful
    pages are skipped on resume unless force=True; failed/empty/unprocessed pages
    remain distinguishable and can be retried. This function never downloads.
    """
    for callback in (ocr, layout_ocr, cancelled, progress):
        if callback is not None and not callable(callback):
            raise ValueError('処理用のコールバックが不正です。')
    saved = store.document(document)
    identity = saved['id']
    counts = {'processed': 0, 'skipped': 0, 'succeeded': 0, 'failed': 0,
              'empty': 0, 'not_processed': 0, 'flagged': 0}

    def report(message):
        if progress is not None:
            progress(message)

    def stopped():
        return cancelled is not None and cancelled()

    def finish(status, message):
        if status != 'excluded':
            store.set_processing(identity, status, message)
        current = store.document(identity)
        return {'document': identity, 'status': status, 'message': message,
                'scope_status': current['scope_status'], **counts}

    try:
        if saved['scope_status'] == 'excluded':
            return finish('excluded', '人が公告の対象外に設定したPDFです。')
        if stopped():
            return finish('cancelled', '処理を中断しました。原本と保存済みの結果は保持しています。')
        store.set_processing(identity, 'processing', '公告範囲と読み取り処理を確認しています。')
        if saved['scope_status'] == 'unconfirmed':
            from notice_scope import find_notice_start
            report(saved['original_name'] + ': 公告開始の候補を探しています。')
            result = find_notice_start(Path(saved['path']), ocr=ocr, layout_ocr=layout_ocr,
                                       cancelled=cancelled,
                                       progress=lambda page, total, method: report(
                                           f'{saved["original_name"]}: 開始候補を確認中 {page}/{total}ページ'))
            if result.get('cancelled') or stopped():
                return finish('cancelled', '開始候補の探索を中断しました。原本は保存済みです。')
            detail = ' / '.join([result.get('reason', '')] + result.get('warnings', []))
            if result.get('start_page') is None:
                store.set_scope_detail(identity, detail or '公告範囲を判断できませんでした。')
                return finish('needs_review', '公告範囲が不明です。保存した原文から人が範囲を設定してください。' + detail)
            start = result['start_page']
            if type(start) is not int or not 1 <= start <= saved['page_count']:
                raise ValueError('開始候補のページ番号が不正です。')
            saved = store.set_provisional_scope(identity, range(start, saved['page_count'] + 1), detail)
        selected = saved['scope_pages']
        if not selected:
            return finish('needs_review', '公告ページが設定されていません。原文から人が範囲を設定してください。')
        ledger = store.ledger()
        from ocr_review import update_ledger_flags

        def analyze(path, page, base_text):
            # The quality module is optional until structured OCR is requested.
            from ocr_review import analyze_page
            return analyze_page(path, page, layout_ocr, base_text=base_text, ledger=ledger)

        for index, number in enumerate(selected, 1):
            if stopped():
                return finish('cancelled', '処理を中断しました。保存済みのページから再開できます。')
            report(f'{saved["original_name"]}: {number}ページを処理中（対象 {index}/{len(selected)}）')
            page = store.process_page(identity, number, ocr=ocr, force=force,
                                      analyze=analyze if layout_ocr is not None else None)
            if page['skipped']:
                counts['skipped'] += 1
            else:
                counts['processed'] += 1
            counts[page['machine_status']] += 1
            current_analysis = update_ledger_flags(page['text'], json.loads(page['analysis_json']), ledger)
            if current_analysis.get('flags'):
                counts['flagged'] += 1
        if stopped():
            return finish('cancelled', '処理を中断しました。処理済みのページは保存されています。')
        if counts['failed'] or counts['empty'] or counts['not_processed'] or counts['flagged']:
            return finish('needs_review', f'処理結果を保存しました。失敗 {counts["failed"]}、文字なし {counts["empty"]}、'
                          f'未処理 {counts["not_processed"]}、確認が必要なページ {counts["flagged"]}。原文確認が必要です。')
        return finish('completed', '読み取り処理を完了しました。公告範囲と本文は人の確認が必要です。')
    except Exception as error:
        return finish('failed', '文書処理に失敗しました。原本と保存済みの結果は保持しています: ' + str(error))


def process_documents(store, documents, **options):
    """Continue after individual document failures; an explicit cancellation stops."""
    results = []
    for document in documents:
        try:
            result = process_document(store, document, **options)
        except Exception as error:
            identity = document.get('id') if isinstance(document, dict) else document
            message = '文書処理に失敗しました: ' + str(error)
            try:
                store.set_processing(identity, 'failed', message)
            except Exception:
                pass
            result = {'document': identity, 'status': 'failed', 'message': message}
        results.append(result)
        if result['status'] == 'cancelled':
            break
    return results
