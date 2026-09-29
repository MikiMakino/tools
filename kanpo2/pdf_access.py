"""Open public PDFs that allow viewing with an empty password."""


def ensure_readable(reader):
    """Accept ordinary public reading; never guess a nonempty password."""
    if not reader.is_encrypted:
        return
    try:
        opened = reader.decrypt('')
    except Exception as exc:
        raise ValueError('PDFを読み取れません。暗号方式に必要なライブラリやPDFの状態を確認してください。') from exc
    if not opened:
        raise ValueError('閲覧パスワードが必要なPDFは取り込めません。パスワードなしで閲覧できる原文PDFを選んでください。')
