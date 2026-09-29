import copy
from pathlib import Path
import unittest
from unittest.mock import Mock

from ocr_review import analyze_layout, analyze_page, match_with_review, update_ledger_flags, _near_name, _norm


def view(text, rotation=0, box=None):
    return {'rotation': rotation, 'region': [0, 0, 1, 1], 'text': text, 'lines': [
        {'text': text, 'bbox': box or {'x': .1, 'y': .2, 'width': .3, 'height': .02}}] if text else []}


class OCRReviewTests(unittest.TestCase):
    def test_wrong_orientation_garbage_does_not_warn_about_readable_name(self):
        layout = {'views': [view('株式会社架空星空工房'), view('尢戸◆/→', 90), view('口ーコ↑', 180)]}
        result = analyze_layout(layout)
        self.assertEqual(result['flags'], [])
        self.assertEqual(result['text'], '株式会社架空星空工房')
        self.assertEqual(result['status'], 'read')

    def test_rotated_supplement_is_local_evidence_and_keeps_original_text(self):
        layout = {'views': [view('読取断片'), view('株式会社架空星空工房', 270)]}
        result = analyze_layout(layout, base_text='元の文字を保持')
        self.assertTrue(result['text'].startswith('元の文字を保持'))
        self.assertIn('株式会社架空星空工房', result['text'])
        self.assertEqual(result['flags'][0]['kind'], 'rotated_supplement')
        self.assertEqual(result['flags'][0]['bbox'], layout['views'][1]['lines'][0]['bbox'])
        self.assertNotIn('confidence', result)
        self.assertEqual(result['machine_status'], 'succeeded')

    def test_confusable_name_is_only_approximate_and_does_not_change_text(self):
        text = '株式会社架空工フォ-ト'
        ledger = [{'code': '001', 'name': '株式会社架空エフォート', 'address': ''}]
        result = analyze_layout({'views': [view(text)]}, ledger=ledger)
        found = match_with_review(result['text'], ledger, result)
        self.assertEqual(result['text'], text)
        self.assertEqual(found[0]['match_type'], 'approximate')
        self.assertEqual(result['flags'][0]['kind'], 'approximate_name')
        self.assertIsNotNone(found[0]['bbox'])

    def test_short_different_name_is_not_fuzzily_matched(self):
        ledger = [{'code': '001', 'name': '株式会社山田', 'address': ''}]
        self.assertEqual(match_with_review('株式会社山川', ledger), [])

    def test_adjacent_columns_cannot_manufacture_company_name(self):
        first = view('株式会社架空', box={'x': .1, 'y': .2, 'width': .2, 'height': .02})
        first['lines'].append({'text': '星空工房', 'bbox': {'x': .7, 'y': .22, 'width': .2, 'height': .02}})
        ledger = [{'code': '001', 'name': '株式会社架空星空工房', 'address': ''}]
        analysis = analyze_layout({'views': [first]}, ledger=ledger)
        self.assertEqual(match_with_review(analysis['text'], ledger, analysis), [])

    def test_retry_is_bounded_and_does_not_mutate_cached_layout(self):
        base = {'views': [view('株式会社架空ティーネットジャ/ヾン')]}
        old = copy.deepcopy(base)
        improved = {'views': [view('株式会社架空ティーネットジャパン')]}
        callback = Mock(side_effect=[base, improved])
        ledger = [{'code': '001', 'name': '株式会社架空ティーネットジャパン', 'address': ''}]
        result = analyze_page(Path('unused.pdf'), 1, callback, ledger=ledger)
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(base, old)
        self.assertEqual(len(result['region_retries']), 1)
        self.assertEqual(match_with_review(result['text'], ledger, result)[0]['match_type'], 'exact')
        self.assertTrue(result['flags'])
        self.assertEqual(callback.call_args.kwargs['rotations'], (0,))
        self.assertLessEqual(callback.call_args.kwargs['timeout'], 30)

    def test_failure_preserves_base_text_and_has_no_fabricated_location(self):
        result = analyze_page(Path('unused.pdf'), 1, Mock(side_effect=RuntimeError('test failure')), base_text='元の文字')
        self.assertEqual(result['text'], '元の文字')
        self.assertEqual(result['machine_status'], 'failed')
        self.assertIsNone(result['flags'][0]['bbox'])

    def test_empty_ocr_is_reported_even_when_extracted_header_exists(self):
        result = analyze_layout({'views': [view('')]}, base_text='官報のヘッダー')
        self.assertEqual(result['text'], '官報のヘッダー')
        self.assertEqual(result['machine_status'], 'empty')
        self.assertEqual(result['flags'][0]['kind'], 'empty_ocr')

    def test_nonempty_pdf_header_does_not_discard_normal_ocr_body(self):
        result = analyze_layout({'views': [view('本文にある株式会社架空星空工房')]}, base_text='官報のヘッダー')
        self.assertIn('官報のヘッダー', result['text'])
        self.assertIn('本文にある株式会社架空星空工房', result['text'])
        again = analyze_layout({'views': [view('本文にある株式会社架空星空工房')]}, base_text=result['text'])
        self.assertEqual(again['text'], result['text'])

    def test_single_manually_chosen_rotation_keeps_general_body_text(self):
        result = analyze_layout({'views': [view('選択した領域の一般的な説明です。', 90)]})
        self.assertEqual(result['text'], '選択した領域の一般的な説明です。')

    def test_ledger_change_replaces_only_approximate_flags_and_keeps_evidence(self):
        old_ledger = [{'code': 'old', 'name': '株式会社架空エフォート', 'address': ''}]
        analysis = analyze_layout({'views': [view('株式会社架空工フォ-ト')]}, ledger=old_ledger)
        analysis['flags'].append({'kind': 'region_failed', 'reason': '維持する理由', 'text': '', 'bbox': None})
        old = copy.deepcopy(analysis)
        changed = update_ledger_flags(analysis['text'], analysis, [])
        self.assertEqual(analysis, old)
        self.assertEqual(changed['text'], analysis['text'])
        self.assertEqual(changed['evidence'], analysis['evidence'])
        self.assertEqual([f['kind'] for f in changed['flags']], ['region_failed'])
        new_ledger = [{'code': 'new', 'name': '株式会社架空エフォート', 'address': ''}]
        matches = match_with_review(analysis['text'], new_ledger, changed)
        refreshed = update_ledger_flags(analysis['text'], changed, new_ledger, matches=matches)
        self.assertEqual([f['ledger_code'] for f in refreshed['flags'] if f['kind']=='approximate_name'], ['new'])

    def test_index_preserves_approximate_candidate_decisions(self):
        observations = ['株式会社架空工フォ-ト', '株式会社星空研究所', '株式会社架空ティーネットジャ/ヾン',
                        'エヌエイチェー株式会社', '株式会社青雲工房', '協同組合架空わかば会']
        names = ['株式会社架空エフォート', '株式会社星空研穹所', '株式会社架空ティーネットジャパン',
                 'エヌエイチエー株式会社', '株式会社青雲工防', '協同組合架空わかは会',
                 '株式会社無関係の会社', '株式会社山田']
        evidence = [{'text': text, 'bbox': None} for text in observations]
        ledger = [{'code': str(i), 'name': name, 'address': ''} for i, name in enumerate(names)]
        expected = {str(i) for i, name in enumerate(names)
                    if any(_near_name(_norm(name), _norm(text)) for text in observations)}
        actual = match_with_review('\n'.join(observations), ledger, {'evidence': evidence})
        self.assertEqual({row['code'] for row in actual if row['match_type']=='approximate'}, expected)


if __name__ == '__main__':
    unittest.main()
