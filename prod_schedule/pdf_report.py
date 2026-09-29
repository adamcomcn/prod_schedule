"""Bilingual (Chinese / English) PDF inspection report.

Uses ReportLab. Chinese text is rendered with the Adobe CJK font
STSong-Light, which PDF viewers supply themselves (no font file to ship).
To embed a TrueType font instead, set PDF_FONT_PATH to a .ttf file (e.g. a
Noto Sans SC static TTF) or place it at static/fonts/report.ttf.
"""
import io
import os
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
NAVY = colors.HexColor('#1a3a5c')
GREY = colors.HexColor('#6b7280')
LINE = colors.HexColor('#d0d5dd')
LIGHT = colors.HexColor('#f1f5f9')
RESULT_COLORS = {
    'Pass': (colors.HexColor('#d1fae5'), colors.HexColor('#065f46')),
    'Fail': (colors.HexColor('#fee2e2'), colors.HexColor('#991b1b')),
}
AMBER = (colors.HexColor('#fef3c7'), colors.HexColor('#92400e'))
RESULT_ZH = {'Pass': '合格', 'Fail': '不合格', 'Partial Pass': '部分合格', 'N/A': '不适用',
             'Conditional Pass': '有条件合格'}

_FONT = None


# TrueType (glyf) CJK fonts, embedded as a subset so every PDF viewer shows
# Chinese. CFF-based .otf / Noto CJK .ttc files are not supported by ReportLab.
FONT_CANDIDATES = [
    os.path.join(BASE_DIR, 'static', 'fonts', 'NotoSansSC-Regular.ttf'),
    os.path.join(BASE_DIR, 'static', 'fonts', 'report.ttf'),
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    '/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc',
    r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\simhei.ttf',
    '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
]


def _font():
    """Register the report font once and return its name."""
    global _FONT
    if _FONT:
        return _FONT
    for path in [os.environ.get('PDF_FONT_PATH', '')] + FONT_CANDIDATES:
        if not path or not os.path.exists(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont('ReportFont', path, subfontIndex=0))
            _FONT = 'ReportFont'
            return _FONT
        except Exception:
            continue
    # Last resort: Adobe CJK font supplied by the viewer (not all viewers have it).
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    _FONT = 'STSong-Light'
    return _FONT


def font_is_embedded():
    return _font() == 'ReportFont'


def _styles():
    f = _font()
    return {
        'title': ParagraphStyle('title', fontName=f, fontSize=16, leading=20, textColor=NAVY),
        'sub': ParagraphStyle('sub', fontName=f, fontSize=8.5, leading=11, textColor=GREY),
        'h': ParagraphStyle('h', fontName=f, fontSize=11, leading=14, textColor=NAVY,
                            spaceBefore=8, spaceAfter=4, keepWithNext=1),
        'label': ParagraphStyle('label', fontName=f, fontSize=8, leading=10, textColor=GREY),
        'cell': ParagraphStyle('cell', fontName=f, fontSize=9, leading=12),
        'small': ParagraphStyle('small', fontName=f, fontSize=7.5, leading=9.5, textColor=GREY),
        'result': ParagraphStyle('result', fontName=f, fontSize=13, leading=17, alignment=1),
    }


def _p(text, style):
    text = escape(str(text if text not in (None, '') else '—')).replace('\n', '<br/>')
    return Paragraph(text, style)


def _bi(zh, en):
    return f'{zh}  {en}'


def _result_text(result):
    result = result or ''
    zh = RESULT_ZH.get(result)
    return f'{zh} {result}' if zh else (result or '—')


def _result_colors(result):
    return RESULT_COLORS.get(result, AMBER)


def _kv_table(pairs, st, col_widths):
    """pairs: [(label, value), ...] laid out as label/value columns, 2 per row."""
    rows, row = [], []
    for label, value in pairs:
        row += [_p(label, st['label']), _p(value, st['cell'])]
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row + [''] * (4 - len(row)))
    table = Table(rows, colWidths=col_widths)
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, -1), 0.25, LINE),
        ('BACKGROUND', (0, 0), (0, -1), LIGHT),
        ('BACKGROUND', (2, 0), (2, -1), LIGHT),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    return table


def _photo(path, max_w, max_h):
    """Downscale a photo (respecting EXIF rotation) and return an Image flowable."""
    from PIL import Image as PILImage, ImageOps
    with PILImage.open(path) as im:
        im = ImageOps.exif_transpose(im).convert('RGB')
        im.thumbnail((1400, 1400))
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=72, optimize=True)
        w, h = im.size
    buf.seek(0)
    scale = min(max_w / w, max_h / h)
    return Image(buf, width=w * scale, height=h * scale)


def build_inspection_pdf(job, record, report_no, attachments=(), defect_names=None,
                         evidence_labels=None, checklist=None, logo_path=None, generated_by=''):
    """Return the PDF as bytes.

    job:        job details dict (schedule columns + region)
    record:     one inspection record from inspections_cache.json
    attachments: rows with evidence_type, original_name, file_path
    defect_names: {code: (name_en, name_cn)}
    evidence_labels: {type: (label_zh, label_en)}
    checklist:  optional {'name', 'inspector', 'date', 'overall', 'summary',
                          'rows': [(section, question, result, notes)]}
    """
    st = _styles()
    defect_names = defect_names or {}
    evidence_labels = evidence_labels or {}
    buf = io.BytesIO()
    page_w, page_h = A4
    margin = 15 * mm
    content_w = page_w - 2 * margin
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M')

    def decorate(canvas, doc):
        canvas.saveState()
        if logo_path and os.path.exists(logo_path):
            try:
                # The logo is white + red, so give it a navy plate.
                canvas.setFillColor(NAVY)
                canvas.roundRect(margin, page_h - 12.5 * mm, 34 * mm, 9 * mm, 1.5 * mm, stroke=0, fill=1)
                canvas.drawImage(logo_path, margin + 2 * mm, page_h - 11.5 * mm, width=30 * mm,
                                 height=7 * mm, preserveAspectRatio=True, anchor='c', mask='auto')
            except Exception:
                pass
        canvas.setFont(_font(), 8)
        canvas.setFillColor(GREY)
        canvas.drawRightString(page_w - margin, page_h - 10 * mm, f'{report_no}')
        canvas.setStrokeColor(LINE)
        canvas.line(margin, page_h - 13.5 * mm, page_w - margin, page_h - 13.5 * mm)
        canvas.drawString(margin, 9 * mm, f'生成时间 Generated: {generated_at}'
                          + (f'  ·  {generated_by}' if generated_by else ''))
        canvas.drawRightString(page_w - margin, 9 * mm, f'第 {doc.page} 页  Page {doc.page}')
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margin, rightMargin=margin,
                            topMargin=19 * mm, bottomMargin=16 * mm,
                            title=f'Inspection Report {report_no}', author=generated_by or 'QC')
    story = []

    # ── Title + overall result ──────────────────────────────────────────────
    result = record.get('result', '')
    bg, fg = _result_colors(result)
    result_style = ParagraphStyle('r', parent=st['result'], textColor=fg)
    head = Table([[
        [_p('检验报告  Inspection Report', st['title']),
         _p(f"{job.get('region', '')}  ·  {job.get('Order Number', '') or record.get('order_number', '')}"
            f"  ·  {job.get('Item Code', '') or record.get('item_code', '')}", st['sub'])],
        _p(_result_text(result), result_style),
    ]], colWidths=[content_w - 58 * mm, 58 * mm])
    head.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BACKGROUND', (1, 0), (1, 0), bg),
        ('BOX', (1, 0), (1, 0), 0.8, fg),
        ('TOPPADDING', (1, 0), (1, 0), 8), ('BOTTOMPADDING', (1, 0), (1, 0), 8),
    ]))
    story += [head, Spacer(1, 6 * mm)]

    widths = [28 * mm, content_w / 2 - 28 * mm, 28 * mm, content_w / 2 - 28 * mm]

    # ── Job details ─────────────────────────────────────────────────────────
    story.append(_p(_bi('订单信息', 'Job details'), st['h']))
    po = job.get('Daemco Purchase Order') or job.get('Purchase Order') or ''
    story.append(_kv_table([
        (_bi('区域', 'Region'), job.get('region') or record.get('region')),
        (_bi('订单号', 'Order No.'), job.get('Order Number') or record.get('order_number')),
        (_bi('采购单号', 'PO'), po),
        (_bi('物料编码', 'Item code'), job.get('Item Code') or record.get('item_code')),
        (_bi('物料描述', 'Description'), job.get('Item Description') or record.get('item_description')),
        (_bi('供应商', 'Supplier'), job.get('Supplier') or job.get('Foundry') or record.get('supplier')),
        (_bi('订单数量', 'Order qty'), job.get('Quantity') or record.get('quantity_ordered')),
        (_bi('预计完成', 'Est. completion'), job.get('Estimated Completion Date')),
        (_bi('最迟出货', 'Must ship'), job.get('Must Ship Date')),
        (_bi('当前状态', 'Status'), job.get('Current Status')),
    ], st, widths))

    # ── Inspection summary ──────────────────────────────────────────────────
    story.append(_p(_bi('检验结果', 'Inspection'), st['h']))
    qty_ins, qty_ok = record.get('quantity_inspected', ''), record.get('quantity_passed', '')
    rate = ''
    try:
        if qty_ins and qty_ok not in ('', None) and float(qty_ins) > 0:
            rate = f'{float(qty_ok) / float(qty_ins) * 100:.1f}%'
    except (TypeError, ValueError):
        pass
    story.append(_kv_table([
        (_bi('检验员', 'Inspector'), record.get('inspector_name')),
        (_bi('检验日期', 'Date'), record.get('inspection_date')),
        (_bi('抽检数量', 'Qty inspected'), qty_ins),
        (_bi('合格数量', 'Qty passed'), f'{qty_ok}  ({rate})' if rate else qty_ok),
        (_bi('总体结果', 'Result'), _result_text(result)),
        (_bi('包装状况', 'Packing'), record.get('packing_condition')),
        (_bi('标识标签', 'Marking'), record.get('marking')),
        (_bi('提交时间', 'Submitted'), (record.get('submitted_at') or '')[:16].replace('T', ' ')),
    ], st, widths))

    # ── Evidence ────────────────────────────────────────────────────────────
    evidence = record.get('evidence') or {}
    if evidence:
        story.append(_p(_bi('证据与测试', 'Evidence & tests'), st['h']))
        rows = [[_p(_bi('项目', 'Item'), st['label']), _p(_bi('结果', 'Result'), st['label']),
                 _p(_bi('文件', 'Files'), st['label']), _p(_bi('备注', 'Notes'), st['label'])]]
        styles = []
        for i, (etype, data) in enumerate(evidence.items(), start=1):
            zh, en = evidence_labels.get(etype, (etype.upper(), etype.upper()))
            res = data.get('result', '')
            rows.append([_p(f'{zh}\n{en}', st['cell']), _p(_result_text(res) if res else '—', st['cell']),
                         _p('\n'.join(data.get('files') or []) or '—', st['small']),
                         _p(data.get('notes'), st['cell'])])
            if res in RESULT_COLORS:
                styles.append(('BACKGROUND', (1, i), (1, i), RESULT_COLORS[res][0]))
        t = Table(rows, colWidths=[45 * mm, 28 * mm, 55 * mm, content_w - 128 * mm], repeatRows=1)
        t.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.25, LINE), ('BACKGROUND', (0, 0), (-1, 0), LIGHT),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ] + styles))
        story.append(t)

    # ── Defects ─────────────────────────────────────────────────────────────
    codes = record.get('defect_codes') or []
    if codes or record.get('defects'):
        story.append(_p(_bi('发现的缺陷', 'Defects found'), st['h']))
        if codes:
            rows = [[_p(_bi('代码', 'Code'), st['label']), _p('缺陷 Defect', st['label'])]]
            for code in codes:
                en, cn = defect_names.get(code, ('', ''))
                rows.append([_p(code, st['cell']), _p(' / '.join(x for x in (cn, en) if x) or '—', st['cell'])])
            t = Table(rows, colWidths=[22 * mm, content_w - 22 * mm], repeatRows=1)
            t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.25, LINE),
                                   ('BACKGROUND', (0, 0), (-1, 0), LIGHT),
                                   ('TEXTCOLOR', (0, 1), (0, -1), colors.HexColor('#991b1b'))]))
            story.append(t)
        if record.get('defects'):
            story += [Spacer(1, 2 * mm), _p(record.get('defects'), st['cell'])]

    if record.get('notes'):
        story += [_p(_bi('备注', 'Notes'), st['h']), _p(record.get('notes'), st['cell'])]

    # ── Checklist (latest digital checklist for this job) ───────────────────
    if checklist:
        story.append(_p(_bi('检验清单', 'Checklist') + f" — {checklist.get('name', '')}", st['h']))
        story.append(_p(f"{checklist.get('inspector', '')}  ·  {checklist.get('date', '')}  ·  "
                        f"{_result_text(checklist.get('overall', ''))}", st['sub']))
        rows = [[_p(_bi('项目', 'Section'), st['label']), _p(_bi('检验要求', 'Guideline'), st['label']),
                 _p(_bi('结果', 'Result'), st['label']), _p(_bi('备注', 'Notes'), st['label'])]]
        styles = []
        for i, (section, question, res, notes) in enumerate(checklist.get('rows', []), start=1):
            rows.append([_p(section, st['small']), _p(question, st['small']),
                         _p(_result_text(res) if res else '—', st['small']), _p(notes, st['small'])])
            if res == 'Fail':
                styles.append(('BACKGROUND', (0, i), (-1, i), RESULT_COLORS['Fail'][0]))
        t = Table(rows, colWidths=[30 * mm, content_w - 88 * mm, 24 * mm, 34 * mm], repeatRows=1)
        t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.25, LINE),
                               ('BACKGROUND', (0, 0), (-1, 0), LIGHT),
                               ('VALIGN', (0, 0), (-1, -1), 'TOP')] + styles))
        story.append(t)
        if checklist.get('summary'):
            story += [Spacer(1, 2 * mm), _p(checklist['summary'], st['cell'])]

    # ── Photos ──────────────────────────────────────────────────────────────
    photos, other_files = [], []
    for att in attachments:
        ext = os.path.splitext(att['original_name'] or att.get('saved_name', ''))[1].lower()
        path = att['file_path']
        if ext in PHOTO_EXTENSIONS and path and os.path.exists(path):
            photos.append(att)
        else:
            other_files.append(att)
    if photos:
        story.append(_p(_bi('现场照片', 'Photos'), st['h']))
        cell_w = (content_w - 6 * mm) / 2
        cells = []
        for att in photos:
            zh, en = evidence_labels.get(att['evidence_type'], (att['evidence_type'], ''))
            try:
                img = _photo(att['file_path'], cell_w, 75 * mm)
            except Exception:
                other_files.append(att)
                continue
            cells.append([img, _p(f"{zh} {en} — {att['original_name']}", st['small'])])
        grid = [cells[i:i + 2] + [''] * (2 - len(cells[i:i + 2])) for i in range(0, len(cells), 2)]
        if grid:
            t = Table(grid, colWidths=[cell_w + 3 * mm] * 2)
            t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                   ('BOTTOMPADDING', (0, 0), (-1, -1), 6)]))
            story.append(t)
    if other_files:
        story.append(_p(_bi('其他附件（请在系统中查看）', 'Other attachments (view in the system)'), st['h']))
        for att in other_files:
            zh, en = evidence_labels.get(att['evidence_type'], (att['evidence_type'], ''))
            story.append(_p(f"• {zh} {en}: {att['original_name']}", st['small']))

    # ── Sign-off ────────────────────────────────────────────────────────────
    sign = Table([[
        _p(_bi('检验员签字', 'Inspector signature') + '\n\n\n______________________', st['cell']),
        _p(_bi('审核', 'Reviewed by') + '\n\n\n______________________', st['cell']),
        _p(_bi('日期', 'Date') + '\n\n\n______________________', st['cell']),
    ]], colWidths=[content_w / 3] * 3)
    story += [Spacer(1, 8 * mm), KeepTogether(sign)]

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()
