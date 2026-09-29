from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from notice_scope import parse_page_ranges, format_page_ranges, find_notice_start, notice_layout_evidence
from pypdf import PdfWriter


class ScopeInputTests(unittest.TestCase):
    def test_disjoint_ranges(self):
        self.assertEqual(parse_page_ranges('3-5,9', 12), [3, 4, 5, 9])

    def test_japanese_typing(self):
        self.assertEqual(parse_page_ranges('３〜５、９', 12), [3, 4, 5, 9])

    def test_overlaps_and_order(self):
        self.assertEqual(parse_page_ranges('5,2-4,3-5', 12), [2, 3, 4, 5])

    def test_invalid_input_does_not_mean_all_pages(self):
        for value in ('', '全部', '0', '-1', '6-3', '1-13', '2,', '1.5', '1,,2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_page_ranges(value, 12)

    def test_invalid_total(self):
        for total in (0, -1, True, '12'):
            with self.subTest(total=total), self.assertRaises(ValueError):
                parse_page_ranges('1', total)

    def test_formatter_round_trip(self):
        pages = [1, 2, 3, 7, 9, 10]
        self.assertEqual(format_page_ranges(pages), '1-3,7,9-10')
        self.assertEqual(parse_page_ranges(format_page_ranges(pages), 10), pages)
        self.assertEqual(format_page_ranges([]), '')


class _NoticeFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pdf = Path(self.temp.name) / 'original.pdf'
        self.pdf.write_bytes(b'unchanged synthetic PDF fixture')

    def scan(self, texts, **kwargs):
        pages = [Mock(extract_text=Mock(side_effect=value) if isinstance(value, Exception)
                      else Mock(return_value=value)) for value in texts]
        reader = SimpleNamespace(is_encrypted=False, pages=pages)
        with patch('pypdf.PdfReader', return_value=reader):
            return find_notice_start(self.pdf, **kwargs)


class NoticeHeadingTests(_NoticeFixture, unittest.TestCase):
    def test_isolated_heading_with_section_is_only_a_candidate(self):
        progress = Mock()
        ocr = Mock(side_effect=['法律の本文', '公告\n諸事項\n裁判所'])
        result = self.scan(['法律\n公告しなければならない。', '公告\n諸事項\n裁判所\n破産手続開始'],
                           ocr=ocr, progress=progress)
        self.assertEqual(result['start_page'], 2)
        self.assertEqual(result['total_pages'], 2)
        self.assertEqual(result['scanned_pages'], 2)
        self.assertEqual(result['method'], 'text')
        self.assertIn('原文', result['reason'])
        self.assertEqual(result['warnings'], [])
        self.assertEqual(progress.call_args.args, (2, 2, 'ocr'))
        self.assertEqual([call.args[1] for call in ocr.call_args_list], [1, 2])
        self.assertEqual(self.pdf.read_bytes(), b'unchanged synthetic PDF fixture')
        self.assertEqual(list(self.pdf.parent.iterdir()), [self.pdf])

    def test_heading_supports_spaces_two_lines_and_court_notice_context(self):
        for heading in ('公 告', '公\n告', '公\n\n告'):
            with self.subTest(heading=heading):
                result = self.scan([heading + '\n諸\n事\n項\n相続財産清算人の選任及び相続権主張の催告'])
                self.assertEqual(result['start_page'], 1)
        result = self.scan(['公告\n相続財産清算人の選任及び相続権主張の催告'])
        self.assertEqual(result['start_page'], 1)

    def test_word_in_law_and_unsubstantiated_heading_are_not_candidates(self):
        for text in ('公告しなければならない。\n裁判所', '前項の公告\n諸事項',
                     '公告\n前項の規定により裁判所が判断する。', '公告', '公\n別の文章\n告\n裁判所',
                     '公告\n相続財産清算人の選任及び相続権主張の催告に関する規定を改正する。',
                     '公告\n第十条の破産手続開始に関する規定を適用する。'):
            with self.subTest(text=text):
                result = self.scan([text])
                self.assertIsNone(result['start_page'])
                self.assertIsNone(result['method'])
                self.assertIn('手動', result['reason'])

    def test_contents_and_page_references_are_not_start_candidates(self):
        for text in ('目次\n公告\n諸事項\n裁判所', '目 次\n公告\n諸事項',
                     '目\n次\n公告\n諸事項', '公告\n8\n諸事項',
                     '公告\n諸事項……8', '公告\n諸事項\n裁判所8',
                     '公告\n裁\n判\n所\n8', '公告\n相続財産清算人の選任……8',
                     '公告\n諸事項\n裁判所\n（8）', '公告\n諸事項\n裁判所(8)',
                     '公告\n相続財産清算人の選任\n8'):
            with self.subTest(text=text):
                result = self.scan([text, '公告\n諸事項\n裁判所'])
                self.assertEqual(result['start_page'], 2)

    def test_text_and_ocr_are_checked_page_by_page(self):
        progress = Mock()
        ocr = Mock(side_effect=['法律', '公 告\n裁判所\n破産手続開始'])
        result = self.scan(['', '', '通常の文章'], ocr=ocr, progress=progress)
        self.assertEqual(result['start_page'], 2)
        self.assertEqual(result['method'], 'ocr')
        self.assertEqual(result['text_scanned_pages'], 2)
        self.assertEqual(result['ocr_scanned_pages'], 2)
        self.assertEqual(result['scanned_pages'], 2)
        self.assertEqual([call.args for call in ocr.call_args_list], [(self.pdf, 1), (self.pdf, 2)])
        self.assertEqual([call.args[2] for call in progress.call_args_list], ['text', 'ocr', 'text', 'ocr'])
        self.assertEqual(result['warnings'], [])

    def test_earlier_image_heading_wins_over_later_extractable_heading(self):
        ocr = Mock(side_effect=['法令本文', '公告\n諸事項\n裁判所\n失踪宣告'])
        result = self.scan(['法律\n告示', '官報のヘッダーだけ', '公告\n諸事項\n裁判所'], ocr=ocr)
        self.assertEqual(result['start_page'], 2)
        self.assertEqual(result['method'], 'ocr')
        self.assertEqual(result['text_scanned_pages'], 2)
        self.assertEqual(result['ocr_scanned_pages'], 2)

    def test_blank_page_insertion_moves_candidate_instead_of_using_fixed_position(self):
        for inserted in (0, 1, 4, 9):
            with self.subTest(inserted=inserted):
                texts = [''] * inserted + ['法令本文', '官報のヘッダーだけ', '公告\n会社その他']
                recognition = [''] * inserted + ['法律', '公 告\n裁判所\n公示送達']
                result = self.scan(texts, ocr=Mock(side_effect=recognition))
                self.assertEqual(result['start_page'], inserted + 2)
                self.assertEqual(result['scanned_pages'], inserted + 2)
                self.assertEqual(result['method'], 'ocr')

    def test_different_notice_titles_and_sections_are_not_tied_to_one_sample(self):
        for context in ('官庁\n入札の公告', '裁判所\n失踪宣告', '会社その他\n株式譲渡について',
                        '地方公共団体\n一般競争入札', '入札公告', '解散公告', '公示送達'):
            for prefix_count in (0, 3, 8):
                with self.subTest(context=context, prefix_count=prefix_count):
                    result = self.scan(['通常の法令本文'] * prefix_count + ['公告\n' + context])
                    self.assertEqual(result['start_page'], prefix_count + 1)

    def test_contents_evidence_in_either_text_or_ocr_vetoes_same_page_candidate(self):
        for text, recognized in (('公告\n諸事項\n裁判所', '目次\n公告8'),
                                 ('目次\n公告\n諸事項', '公告\n諸事項\n裁判所'),
                                 ('公告\n諸事項', '公告（8）\n裁判所（8）')):
            with self.subTest(text=text, recognized=recognized):
                ocr = Mock(side_effect=[recognized, '公告\n裁判所\n失踪宣告'])
                result = self.scan([text, '公告\n裁判所\n失踪宣告'], ocr=ocr)
                self.assertEqual(result['start_page'], 2)

    def test_kanji_page_references_in_vertical_contents_are_rejected(self):
        for recognized in ('公告\n八\n諸事項', '公告八\n裁判所', '公告\n諸事項\n裁判所二六',
                           '公告\n諸事項……二六', '公\n告\n八\n諸事項',
                           '公告\n裁判所\n（八）', '公告\n相続財産清算人の選任……八',
                           '公告\n裁判所\n二六', '公告\n裁判所\n二十六'):
            with self.subTest(recognized=recognized):
                # Partial PDF text alone resembles a heading; the OCR reveals its TOC references.
                ocr = Mock(side_effect=[recognized, '公告\n裁判所\n公示送達'])
                result = self.scan(['公告\n諸事項\n裁判所', '公告\n裁判所'], ocr=ocr)
                self.assertEqual(result['start_page'], 2)

    def test_text_candidate_with_failed_ocr_is_reported_with_warning(self):
        result = self.scan(['公告\n裁判所'], ocr=Mock(side_effect=RuntimeError('OCR unavailable')))
        self.assertEqual(result['start_page'], 1)
        self.assertEqual(result['method'], 'text')
        self.assertTrue(any('OCR失敗' in warning for warning in result['warnings']))

    def test_unreadable_earlier_page_prevents_unqualified_success(self):
        result = self.scan([RuntimeError('extract failed'), '公告\n裁判所'])
        self.assertEqual(result['start_page'], 2)
        self.assertTrue(any('取りこぼし' in warning or '取りこぼした' in warning for warning in result['warnings']))
        result = self.scan(['', ''], ocr=Mock(side_effect=[RuntimeError('OCR failed'), '公告\n裁判所']))
        self.assertEqual(result['start_page'], 2)
        self.assertTrue(any('OCR失敗' in warning for warning in result['warnings']))

    def test_repeated_ocr_failures_stop_with_warning(self):
        ocr = Mock(side_effect=RuntimeError('unavailable'))
        result = self.scan([''] * 5, ocr=ocr)
        self.assertIsNone(result['start_page'])
        self.assertEqual(ocr.call_count, 3)
        self.assertTrue(any('3ページ連続' in warning for warning in result['warnings']))
        self.assertTrue(any('取りこぼし' in warning for warning in result['warnings']))

    def test_invalid_ocr_response_is_failure_and_empty_result_needs_manual_scope(self):
        result = self.scan([''], ocr=lambda path, number: None)
        self.assertIsNone(result['start_page'])
        self.assertTrue(any('文字列' in warning for warning in result['warnings']))
        result = self.scan([''], ocr=lambda path, number: '')
        self.assertIsNone(result['start_page'])
        self.assertIn('手動', result['reason'])
        self.assertTrue(result['warnings'])

    def test_cancellation_before_scan_and_during_ocr_is_explicit(self):
        result = self.scan(['公告\n裁判所'], cancelled=lambda: True)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['scanned_pages'], 0)
        self.assertIsNone(result['start_page'])
        stop = {'value': False}

        def ocr(path, number):
            stop['value'] = True
            return '公告\n裁判所'

        result = self.scan([''], ocr=ocr, cancelled=lambda: stop['value'])
        self.assertTrue(result['cancelled'])
        self.assertIsNone(result['start_page'])
        self.assertTrue(result['warnings'])

    def test_invalid_callbacks_are_rejected(self):
        for option in ('ocr', 'cancelled', 'progress', 'layout_ocr'):
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.scan(['公告\n諸事項'], **{option: True})

    def test_public_empty_password_pdf_can_be_scanned_without_modifying_original(self):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.add_blank_page(width=595, height=842)
        writer.encrypt(user_password='', owner_password='owner-only', algorithm='AES-256')
        writer.write(str(self.pdf))
        original = self.pdf.read_bytes()
        result = find_notice_start(self.pdf, ocr=lambda path, page: '公告\n諸事項' if page == 2 else '法令')
        self.assertEqual(result['start_page'], 2)
        self.assertEqual(result['method'], 'ocr')
        self.assertEqual(self.pdf.read_bytes(), original)

    def test_actual_password_required_pdf_is_rejected_before_scanning(self):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.encrypt(user_password='required', owner_password='owner-only')
        writer.write(str(self.pdf))
        ocr = Mock()
        with self.assertRaises(ValueError):
            find_notice_start(self.pdf, ocr=ocr)
        ocr.assert_not_called()


def layout_line(text, x, y, width, height):
    return {'text': text, 'bbox': {'x': x, 'y': y, 'width': width, 'height': height}}


def layout_view(rotation, lines, text=None):
    return {'region': [0, 0, 1, 1], 'rotation': rotation, 'lines': lines,
            'text': '\n'.join(line['text'] for line in lines) if text is None else text}


def weak_layout():
    # Independent rotations recover different parts; all boxes are in original-page coordinates.
    return {'page': 1, 'coordinate_system': 'normalized_page', 'views': [
        layout_view(0, [layout_line('相続財産清算人の選任及び相続権', .10756, .16837, .13759, .01425),
                        layout_line('主張の催告', .10756, .18370, .07393, .01399)]),
        layout_view(90, [layout_line('公', .13892, .09172, .01444, .02152),
                         layout_line('諸事項', .09445, .14040, .05245, .01399)])]}


class LayoutGeometryTests(_NoticeFixture, unittest.TestCase):
    def test_partial_character_section_and_wrapped_title_across_views_is_weak(self):
        evidence = notice_layout_evidence(weak_layout())
        self.assertEqual(evidence['strength'], 'weak')
        self.assertEqual(evidence['heading']['rotation'], 90)
        self.assertEqual(evidence['title']['rotation'], 0)
        self.assertEqual(evidence['title']['text'], '相続財産清算人の選任及び相続権主張の催告')
        self.assertEqual(len(evidence['title']['parts']), 2)
        self.assertIn('弱い候補', evidence['reason'])

    def test_complete_heading_and_nearby_section_have_stronger_evidence(self):
        layout = weak_layout()
        layout['views'][1]['lines'][0]['text'] = '公告'
        layout['views'][0] = layout_view(0, [])
        evidence = notice_layout_evidence(layout)
        self.assertEqual(evidence['strength'], 'strong')
        self.assertEqual(evidence['heading']['text'], '公告')

    def test_translating_all_labels_preserves_candidate(self):
        for dx, dy in ((0, 0), (.45, .55), (-.05, .3)):
            with self.subTest(dx=dx, dy=dy):
                layout = weak_layout()
                for view in layout['views']:
                    for line in view['lines']:
                        line['bbox']['x'] += dx
                        line['bbox']['y'] += dy
                evidence = notice_layout_evidence(layout)
                self.assertIsNotNone(evidence)
                self.assertEqual(evidence['strength'], 'weak')

    def test_distant_section_or_title_and_neighbouring_column_are_not_joined(self):
        for moved in ('section', 'title', 'neighbouring_section'):
            with self.subTest(moved=moved):
                layout = weak_layout()
                if moved == 'title':
                    changed = layout['views'][0]['lines']
                else:
                    changed = [layout['views'][1]['lines'][1]]
                for line in changed:
                    line['bbox']['x'] += .20 if moved == 'neighbouring_section' else .55
                self.assertIsNone(notice_layout_evidence(layout))

    def test_partial_heading_requires_both_section_and_independent_title(self):
        for missing in ('section', 'title'):
            with self.subTest(missing=missing):
                layout = weak_layout()
                if missing == 'section':
                    layout['views'][1]['lines'] = layout['views'][1]['lines'][:1]
                else:
                    layout['views'][0]['lines'] = []
                self.assertIsNone(notice_layout_evidence(layout))
        layout = weak_layout()
        layout['views'][0]['lines'][0]['text'] = '相続財産清算人の選任及び相続権主張の催告に関する規定を改める。'
        layout['views'][0]['lines'] = layout['views'][0]['lines'][:1]
        self.assertIsNone(notice_layout_evidence(layout))

    def test_different_independent_notice_titles_support_weak_candidate(self):
        for title in ('公示送達', '入札公告', '解散公告', '破産手続開始'):
            with self.subTest(title=title):
                layout = weak_layout()
                layout['views'][0] = layout_view(0, [layout_line(title, .11, .17, .10, .02)])
                evidence = notice_layout_evidence(layout)
                self.assertEqual(evidence['strength'], 'weak')
                self.assertEqual(evidence['title']['text'], title)

    def test_wrapped_title_parts_must_share_view_and_nearby_column(self):
        for separated in ('view', 'column'):
            with self.subTest(separated=separated):
                layout = weak_layout()
                if separated == 'view':
                    second = layout['views'][0]['lines'].pop()
                    layout['views'].append(layout_view(270, [second]))
                else:
                    layout['views'][0]['lines'][1]['bbox']['x'] += .4
                self.assertIsNone(notice_layout_evidence(layout))

    def test_contents_anywhere_in_views_veto_geometry(self):
        for content in ('目次', '裁判所\n八', '会社その他二六', '公告……八'):
            with self.subTest(content=content):
                layout = weak_layout()
                layout['views'].append(layout_view(270, [], text=content))
                self.assertIsNone(notice_layout_evidence(layout))

    def test_invalid_or_missing_boxes_cannot_supply_geometric_evidence(self):
        for bad in (None, [0, 0, .1, .1], {'x': -1, 'y': .1, 'width': .1, 'height': .1},
                    {'x': .1, 'y': .1, 'width': float('nan'), 'height': .1}):
            with self.subTest(bad=bad):
                layout = weak_layout()
                layout['views'][1]['lines'][0]['bbox'] = bad
                self.assertIsNone(notice_layout_evidence(layout))

    def test_layout_ocr_finds_earlier_image_candidate_without_second_ocr(self):
        no_heading = {'coordinate_system': 'normalized_page', 'views': [layout_view(0, [], text='法令本文')]}
        layout_callback = Mock(side_effect=[no_heading, weak_layout()])
        ordinary_ocr = Mock()
        result = self.scan(['ヘッダー', 'ヘッダー', '公告\n諸事項'], ocr=ordinary_ocr, layout_ocr=layout_callback)
        self.assertEqual(result['start_page'], 2)
        self.assertEqual(result['method'], 'ocr')
        self.assertEqual(result['evidence_strength'], 'weak')
        self.assertIn('弱い候補', result['reason'])
        self.assertEqual(result['ocr_scanned_pages'], 2)
        ordinary_ocr.assert_not_called()

    def test_layout_contents_vetoes_otherwise_valid_extracted_heading(self):
        contents = weak_layout()
        contents['views'].append(layout_view(270, [], text='目次\n裁判所八'))
        result = self.scan(['公告\n諸事項', 'ヘッダー'], layout_ocr=Mock(side_effect=[contents, weak_layout()]))
        self.assertEqual(result['start_page'], 2)
        self.assertEqual(result['evidence_strength'], 'weak')

    def test_layout_failure_is_visible_and_does_not_launch_ordinary_ocr(self):
        ordinary = Mock()
        result = self.scan([''], ocr=ordinary, layout_ocr=lambda path, page: {'coordinate_system': 'pixels', 'views': []})
        self.assertIsNone(result['start_page'])
        self.assertTrue(any('座標形式' in warning for warning in result['warnings']))
        ordinary.assert_not_called()

    def test_geometry_rejection_cannot_be_bypassed_by_flattened_text(self):
        for section_x, section_y in ((.8, .8), (.21, .1)):
            with self.subTest(section_x=section_x, section_y=section_y):
                layout = {'coordinate_system': 'normalized_page', 'views': [layout_view(0, [
                    layout_line('公告', .1, .1, .1, .025),
                    layout_line('諸事項', section_x, section_y, .1, .025)])]}
                self.assertIsNone(notice_layout_evidence(layout))
                result = self.scan(['公告\n諸事項'], layout_ocr=lambda path, page: layout)
                self.assertIsNone(result['start_page'])

    def test_partial_character_requires_general_notices_section(self):
        for section in ('官庁', '裁判所', '会社その他', '政府調達'):
            with self.subTest(section=section):
                layout = weak_layout()
                layout['views'][1]['lines'][1]['text'] = section
                self.assertIsNone(notice_layout_evidence(layout))

    def test_weak_title_in_adjacent_column_is_not_combined_with_section(self):
        layout = {'coordinate_system': 'normalized_page', 'views': [layout_view(0, [
            layout_line('公', .1, .1, .015, .02),
            layout_line('諸事項', .1, .15, .05, .02),
            layout_line('破産手続開始', .21, .2, .10, .02)])]}
        self.assertIsNone(notice_layout_evidence(layout))
        self.assertIsNone(self.scan(['ヘッダー'], layout_ocr=lambda path, page: layout)['start_page'])

    def test_extended_section_labels_need_complete_heading_and_are_checked_for_toc(self):
        for section in ('会社その他の公告', '特殊法人等', '政府調達'):
            with self.subTest(section=section):
                layout = {'coordinate_system': 'normalized_page', 'views': [layout_view(0, [
                    layout_line('公告', .1, .1, .1, .025),
                    layout_line(section, .1, .15, .12, .025)])]}
                self.assertEqual(notice_layout_evidence(layout)['strength'], 'strong')
                self.assertEqual(self.scan(['公告\n' + section])['start_page'], 1)
                self.assertIsNone(self.scan([section])['start_page'])
                layout['views'].append(layout_view(90, [], text=section + '二六'))
                self.assertIsNone(notice_layout_evidence(layout))

    def test_vertical_heading_stack_progresses_left_with_wrapped_title(self):
        for dx, dy in ((0, 0), (.45, -.55), (-.10, -.30)):
            with self.subTest(dx=dx, dy=dy):
                layout = {'coordinate_system': 'normalized_page', 'views': [
                    layout_view(0, [layout_line('公', .270 + dx, .795 + dy, .021, .014),
                                    layout_line('諸事項', .228 + dx, .750 + dy, .014, .052)]),
                    layout_view(270, [layout_line('前払式支払手段発行者の発行', .200 + dx, .763 + dy, .014, .138),
                                      layout_line('保証金に係る仮配当表公示', .185 + dx, .763 + dy, .014, .128)])]}
                evidence = notice_layout_evidence(layout)
                self.assertEqual(evidence['strength'], 'weak')
                self.assertEqual(evidence['direction'], 'left')
                self.assertEqual(len(evidence['title']['parts']), 2)
                result = self.scan(['通常の本文'] * 3 + ['ヘッダー'],
                                   layout_ocr=Mock(side_effect=[{'coordinate_system': 'normalized_page', 'views': []}] * 3 + [layout]))
                self.assertEqual(result['start_page'], 4)

    def test_horizontal_adjacent_labels_do_not_become_a_vertical_stack(self):
        layout = {'coordinate_system': 'normalized_page', 'views': [layout_view(0, [
            layout_line('公告', .3, .2, .1, .02),
            layout_line('諸事項', .18, .2, .1, .02)])]}
        self.assertIsNone(notice_layout_evidence(layout))

    def test_heading_section_and_title_cannot_change_flow_direction(self):
        layout = {'coordinate_system': 'normalized_page', 'views': [layout_view(0, [
            layout_line('公', .3, .1, .02, .02),
            layout_line('諸事項', .27, .15, .06, .02),
            layout_line('破産手続開始', .24, .15, .02, .10)])]}
        self.assertIsNone(notice_layout_evidence(layout))

    def test_wrapped_composite_title_is_exact_not_a_prose_substring(self):
        for suffix in ('', 'に関する条文を改正する。'):
            with self.subTest(suffix=suffix):
                layout = weak_layout()
                layout['views'][0] = layout_view(0, [
                    layout_line('破産手続開始及び免責許可申', .107, .171, .137, .014),
                    layout_line('立てに関する意見申述期間' + suffix, .107, .186, .126, .014)])
                evidence = notice_layout_evidence(layout)
                if suffix:
                    self.assertIsNone(evidence)
                else:
                    self.assertEqual(evidence['strength'], 'weak')

    def test_separate_contents_column_can_share_page_with_announcements(self):
        for contents_lines in (
                [layout_line('目次', .08, .08, .12, .025),
                 layout_line('公告', .1, .15, .08, .025),
                 layout_line('諸事項', .1, .20, .08, .025)],
                [layout_line('目', .08, .08, .025, .025),
                 layout_line('次', .14, .08, .025, .025),
                 layout_line('公告', .1, .15, .08, .025),
                 layout_line('諸事項', .1, .20, .08, .025)]):
            with self.subTest(split=len(contents_lines) == 4):
                layout = weak_layout()
                for view in layout['views']:
                    for line in view['lines']:
                        line['bbox']['x'] += .4
                layout['views'].append(layout_view(180, contents_lines))
                evidence = notice_layout_evidence(layout)
                self.assertEqual(evidence['strength'], 'weak')
                self.assertGreater(evidence['heading']['bbox']['x'], .5)
                self.assertTrue(evidence['contents_localized'])
                # Native extraction also saw the TOC; positioned OCR accounts for it.
                self.assertEqual(self.scan(['目次'], layout_ocr=lambda path, page: layout)['start_page'], 1)

    def test_contents_heading_vetoes_a_complete_candidate_in_its_own_column(self):
        layout = weak_layout()
        layout['views'].append(layout_view(180, [layout_line('目次', .09, .04, .16, .025)]))
        self.assertIsNone(notice_layout_evidence(layout))
        layout['views'][1]['lines'][0]['text'] = '公告'
        self.assertIsNone(notice_layout_evidence(layout))

    def test_vertical_contents_column_vetoes_its_own_candidate(self):
        layout = {'coordinate_system': 'normalized_page', 'views': [layout_view(270, [
            layout_line('目次', .5, .2, .02, .12),
            layout_line('公告', .4, .23, .02, .06),
            layout_line('諸事項', .35, .22, .02, .08)])]}
        self.assertIsNone(notice_layout_evidence(layout))

    def test_detached_near_page_reference_vetoes_candidate_but_remote_numeral_does_not(self):
        for near in (True, False):
            with self.subTest(near=near):
                layout = weak_layout()
                layout['views'][0]['lines'].append(layout_line('八', .19 if near else .80, .184 if near else .04, .02, .014))
                layout['views'][0]['text'] = '\n'.join(line['text'] for line in layout['views'][0]['lines'])
                evidence = notice_layout_evidence(layout)
                result = self.scan(['ヘッダー'], layout_ocr=lambda path, page: layout)
                if near:
                    self.assertIsNone(evidence)
                    self.assertIsNone(result['start_page'])
                else:
                    self.assertEqual(evidence['strength'], 'weak')
                    self.assertEqual(result['start_page'], 1)

    def test_unlocated_native_contents_cannot_be_bypassed_by_layout_candidate(self):
        self.assertIsNone(self.scan(['目次\n公告八'], layout_ocr=lambda path, page: weak_layout())['start_page'])

    def test_reference_in_separate_column_does_not_veto_real_heading(self):
        layout = weak_layout()
        layout['views'].append(layout_view(180, [layout_line('公告八', .75, .1, .12, .025)]))
        evidence = notice_layout_evidence(layout)
        self.assertEqual(evidence['strength'], 'weak')
        self.assertEqual(self.scan(['公告八'], layout_ocr=lambda path, page: layout)['start_page'], 1)
