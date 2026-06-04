"""
One-time script: extract defect photos from the PPT and link them to KB articles.
Run once from the prod_schedule folder:  python extract_ppt_images.py
"""
import zipfile, os, sys, sqlite3, shutil, json, posixpath
from xml.etree import ElementTree as ET

sys.stdout.reconfigure(encoding='utf-8')

PPT_PATH = r'C:\Users\JWan\Documents\Revised_DAEMCO Inspection criteria for Murphy.pptx'
OUT_DIR  = r'C:\Users\JWan\Documents\prod_schedule\static\kb_images'
DB_PATH  = r'C:\Users\JWan\Documents\prod_schedule\data\app.db'

# slide number → defect code (slide 1 is the cover, skip it)
SLIDE_CODE = {
     2:'A01',  3:'A02',  4:'A03',  5:'A04',  6:'A05',  7:'A06',
     8:'A07',  9:'A08', 10:'A09', 11:'A10', 12:'A11', 13:'A12',
    14:'A13', 15:'A14', 16:'A15', 17:'A16', 18:'A17', 19:'A18',
    20:'A19', 21:'A20', 22:'A21', 23:'A22', 24:'A23', 25:'A24',
    26:'A25', 27:'A29', 28:'A30', 29:'A31', 30:'A32', 31:'A33',
    32:'A34', 33:'A35', 34:'A36', 35:'A37', 36:'A38', 37:'A39',
    38:'A40', 39:'A41', 40:'A42', 41:'A43', 42:'A45', 43:'A46',
    44:'A47', 45:'A48', 46:'A49', 47:'A50', 48:'A51', 49:'A52',
    50:'A53', 51:'A54', 52:'A55', 53:'A56', 54:'A57',
}

PKG_NS  = 'http://schemas.openxmlformats.org/package/2006/relationships'
IMG_TYPE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image'
# Only raster formats browsers can display; skip EMF/WMF vector files
KEEP_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.bmp'}

os.makedirs(OUT_DIR, exist_ok=True)

# ── Database: add images column if missing ────────────────────────────────────
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
try:
    conn.execute('ALTER TABLE kb_articles ADD COLUMN images TEXT DEFAULT "[]"')
    conn.commit()
    print('Added images column to kb_articles.')
except Exception:
    pass  # column already exists

# Build code → article id map (articles are tagged with their defect code)
code_to_id = {}
for row in conn.execute("SELECT id, tags FROM kb_articles").fetchall():
    for tag in (row['tags'] or '').split(','):
        tag = tag.strip()
        if tag:
            code_to_id[tag] = row['id']

# ── Extract images from PPTX ──────────────────────────────────────────────────
total_imgs = 0
with zipfile.ZipFile(PPT_PATH) as z:
    all_files = set(z.namelist())

    for slide_num in sorted(SLIDE_CODE):
        code = SLIDE_CODE[slide_num]
        rel_path = f'ppt/slides/_rels/slide{slide_num}.xml.rels'
        if rel_path not in all_files:
            print(f'  Slide {slide_num} ({code}): rel file missing — skip')
            continue

        with z.open(rel_path) as f:
            root = ET.parse(f).getroot()

        # Collect image targets for this slide
        img_entries = []
        for rel in root.findall(f'{{{PKG_NS}}}Relationship'):
            if rel.get('Type') != IMG_TYPE:
                continue
            target = rel.get('Target', '')
            # Resolve relative path: target is relative to ppt/slides/
            norm = posixpath.normpath(posixpath.join('ppt/slides', target))
            ext  = posixpath.splitext(norm)[1].lower()
            if ext not in KEEP_EXT:
                continue
            if norm not in all_files:
                continue
            size = z.getinfo(norm).file_size
            img_entries.append((size, norm, ext))

        if not img_entries:
            print(f'  Slide {slide_num} ({code}): no raster images')
            continue

        # Sort largest-first (main defect photos tend to be bigger than logos)
        img_entries.sort(key=lambda x: -x[0])

        out_sub = os.path.join(OUT_DIR, code)
        os.makedirs(out_sub, exist_ok=True)

        saved = []
        for i, (size, norm, ext) in enumerate(img_entries, 1):
            fname = f'img{i:02d}{ext}'
            out_path = os.path.join(out_sub, fname)
            with z.open(norm) as src, open(out_path, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            saved.append(f'{code}/{fname}')

        print(f'  Slide {slide_num} ({code}): {len(saved)} image(s)')
        total_imgs += len(saved)

        # Update DB
        art_id = code_to_id.get(code)
        if art_id:
            conn.execute('UPDATE kb_articles SET images=? WHERE id=?',
                         (json.dumps(saved), art_id))

conn.commit()
conn.close()
print(f'\nDone. {total_imgs} images extracted and linked to knowledge base.')
