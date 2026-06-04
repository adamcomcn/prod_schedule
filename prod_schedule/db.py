import os
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'data', 'app.db')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    contact_person TEXT    DEFAULT '',
    phone          TEXT    DEFAULT '',
    email          TEXT    DEFAULT '',
    address        TEXT    DEFAULT '',
    country        TEXT    DEFAULT 'China',
    notes          TEXT    DEFAULT '',
    created_at     TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS product_categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    DEFAULT '',
    name        TEXT    NOT NULL,
    description TEXT    DEFAULT '',
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS category_inspectors (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id    INTEGER NOT NULL REFERENCES product_categories(id) ON DELETE CASCADE,
    inspector_name TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER REFERENCES product_categories(id) ON DELETE SET NULL,
    supplier_id INTEGER REFERENCES suppliers(id)          ON DELETE SET NULL,
    item_code   TEXT    DEFAULT '',
    name        TEXT    NOT NULL,
    description TEXT    DEFAULT '',
    image_path  TEXT    DEFAULT '',
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT   NOT NULL,
    region      TEXT    DEFAULT '',
    customer    TEXT    DEFAULT '',
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    category_id INTEGER REFERENCES product_categories(id) ON DELETE SET NULL,
    item_code   TEXT    DEFAULT '',
    description TEXT    DEFAULT '',
    quantity    REAL    DEFAULT 0,
    status      TEXT    DEFAULT 'Pending',
    order_date  TEXT    DEFAULT '',
    eta         TEXT    DEFAULT '',
    notes       TEXT    DEFAULT '',
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS defect_codes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    code     TEXT    NOT NULL UNIQUE,
    name     TEXT    NOT NULL,
    name_cn  TEXT    DEFAULT '',
    category TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS employees (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    email        TEXT    DEFAULT '',
    phone        TEXT    DEFAULT '',
    role         TEXT    DEFAULT 'Inspector',
    stationed_at TEXT    DEFAULT '',
    active       INTEGER DEFAULT 1,
    created_at   TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS outstanding_jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key        TEXT    NOT NULL UNIQUE,
    sheet          TEXT    NOT NULL,
    headers_json   TEXT    DEFAULT '[]',
    row_json       TEXT    DEFAULT '[]',
    week_label     TEXT    DEFAULT '',
    order_number   TEXT    DEFAULT '',
    item_code      TEXT    DEFAULT '',
    item_desc      TEXT    DEFAULT '',
    supplier       TEXT    DEFAULT '',
    quantity       TEXT    DEFAULT '',
    est_completion TEXT    DEFAULT '',
    must_ship      TEXT    DEFAULT '',
    shipped_at     TEXT    DEFAULT (datetime('now','localtime')),
    completed      INTEGER DEFAULT 0,
    completed_at   TEXT    DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS inspection_tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key        TEXT    NOT NULL UNIQUE,
    order_number   TEXT    DEFAULT '',
    region         TEXT    DEFAULT '',
    item_code      TEXT    DEFAULT '',
    description    TEXT    DEFAULT '',
    supplier       TEXT    DEFAULT '',
    quantity       TEXT    DEFAULT '',
    est_completion TEXT    DEFAULT '',
    must_ship      TEXT    DEFAULT '',
    status         TEXT    DEFAULT 'Pending',
    week_label     TEXT    DEFAULT '',
    created_at     TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS inspection_attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key       TEXT    NOT NULL,
    insp_index    INTEGER DEFAULT 0,
    evidence_type TEXT    NOT NULL,
    original_name TEXT    DEFAULT '',
    saved_name    TEXT    DEFAULT '',
    file_path     TEXT    DEFAULT '',
    drive_link    TEXT    DEFAULT '',
    result        TEXT    DEFAULT '',
    notes         TEXT    DEFAULT '',
    uploaded_at   TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS kb_categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT    DEFAULT '',
    parent_id   INTEGER REFERENCES kb_categories(id) ON DELETE SET NULL,
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS kb_articles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER REFERENCES kb_categories(id) ON DELETE SET NULL,
    title       TEXT    NOT NULL,
    content     TEXT    DEFAULT '',
    tags        TEXT    DEFAULT '',
    created_at  TEXT    DEFAULT (datetime('now','localtime')),
    updated_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER REFERENCES kb_categories(id) ON DELETE SET NULL,
    article_id  INTEGER REFERENCES kb_articles(id)   ON DELETE SET NULL,
    question    TEXT    NOT NULL,
    option_a    TEXT    DEFAULT '',
    option_b    TEXT    DEFAULT '',
    option_c    TEXT    DEFAULT '',
    option_d    TEXT    DEFAULT '',
    answer      TEXT    NOT NULL,
    explanation TEXT    DEFAULT '',
    difficulty  TEXT    DEFAULT 'Medium',
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS exams (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    description TEXT    DEFAULT '',
    pass_score  INTEGER DEFAULT 60,
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS exam_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id     INTEGER NOT NULL REFERENCES exams(id)     ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    order_num   INTEGER DEFAULT 0,
    UNIQUE(exam_id, question_id)
);

CREATE TABLE IF NOT EXISTS training_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    exam_id     INTEGER REFERENCES exams(id) ON DELETE SET NULL,
    due_date    TEXT    DEFAULT '',
    notes       TEXT    DEFAULT '',
    created_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS form_templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    keywords      TEXT    DEFAULT '',
    sections_json TEXT    DEFAULT '[]',
    source_file   TEXT    DEFAULT '',
    created_at    TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS form_responses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key       TEXT    NOT NULL,
    template_id   INTEGER REFERENCES form_templates(id) ON DELETE SET NULL,
    template_name TEXT    DEFAULT '',
    dpl_number    TEXT    DEFAULT '',
    inspector     TEXT    DEFAULT '',
    insp_date     TEXT    DEFAULT '',
    answers       TEXT    DEFAULT '{}',
    overall       TEXT    DEFAULT '',
    summary       TEXT    DEFAULT '',
    submitted_at  TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS training_assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id      INTEGER NOT NULL REFERENCES training_plans(id) ON DELETE CASCADE,
    employee_id  INTEGER NOT NULL REFERENCES employees(id)      ON DELETE CASCADE,
    status       TEXT    DEFAULT 'Assigned',
    score        INTEGER DEFAULT NULL,
    passed       INTEGER DEFAULT NULL,
    answers      TEXT    DEFAULT '{}',
    completed_at TEXT    DEFAULT NULL,
    UNIQUE(plan_id, employee_id)
);

CREATE TABLE IF NOT EXISTS regions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    level      INTEGER NOT NULL DEFAULT 1,
    parent_id  INTEGER REFERENCES regions(id) ON DELETE CASCADE,
    code       TEXT    DEFAULT '',
    sort_order INTEGER DEFAULT 0,
    created_at TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS employee_work_info (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id        INTEGER NOT NULL UNIQUE REFERENCES employees(id) ON DELETE CASCADE,
    supervisor_id      INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    skill_level        TEXT    DEFAULT '',
    product_categories TEXT    DEFAULT '',
    travel_status      TEXT    DEFAULT '',
    overtime_notes     TEXT    DEFAULT '',
    updated_at         TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS attendance_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id       INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    work_date         TEXT    NOT NULL,
    checkin_time      TEXT    DEFAULT '',
    checkout_time     TEXT    DEFAULT '',
    checkin_location  TEXT    DEFAULT '',
    checkout_location TEXT    DEFAULT '',
    status            TEXT    DEFAULT 'Normal',
    overtime_mins     INTEGER DEFAULT 0,
    notes             TEXT    DEFAULT '',
    created_at        TEXT    DEFAULT (datetime('now','localtime')),
    UNIQUE(employee_id, work_date)
);

CREATE TABLE IF NOT EXISTS leave_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id    INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    leave_type     TEXT    NOT NULL DEFAULT 'Annual',
    start_date     TEXT    NOT NULL,
    end_date       TEXT    NOT NULL,
    days           REAL    DEFAULT 1,
    reason         TEXT    DEFAULT '',
    status         TEXT    DEFAULT 'Pending',
    approved_by    INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    approver_notes TEXT    DEFAULT '',
    created_at     TEXT    DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS expense_claims (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id    INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    claim_date     TEXT    NOT NULL,
    claim_type     TEXT    NOT NULL DEFAULT 'Transport',
    amount         REAL    DEFAULT 0,
    description    TEXT    DEFAULT '',
    invoice_path   TEXT    DEFAULT '',
    status         TEXT    DEFAULT 'Pending',
    approved_by    INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    approver_notes TEXT    DEFAULT '',
    created_at     TEXT    DEFAULT (datetime('now','localtime'))
);
"""

_DEFECT_SEEDS = [
    ('A01','Assembly Issues','装配问题','Assembly'),
    ('A02','Missing Parts','零件缺失','Assembly'),
    ('A51','Lid/Frame Assembly Not to Specification','盖/框架装配不符规格','Assembly'),
    ('A52','Failed Pressure Test / Leaking Product','压力测试失败/产品泄漏','Assembly'),
    ('A04','Hole in Casting','铸件孔洞','Casting'),
    ('A05','Socket Profile Cast Cavity','承口铸腔','Casting'),
    ('A06','Cast Deformity','铸造变形','Casting'),
    ('A07','Lumpy Casting','铸件结块','Casting'),
    ('A08','Flange Faces Not Parallel','法兰面不平行','Casting'),
    ('A09','Pinholes on Rough Surface','粗糙面针孔','Casting'),
    ('A10','Socket Gasket Fitment Issue (Excess Casting)','承口垫圈配合问题（铸造过剩）','Casting'),
    ('A11','Pinholes on Smooth Surface','光滑面针孔','Casting'),
    ('A12','Raised Face Not Flat','凸面不平','Casting'),
    ('A29','Flashing Not Removed','飞边未去除','Casting'),
    ('A42','Spigot Not to Specification','插口不符规格','Casting'),
    ('A49','Sharp Edges / Edges Not Deburred','锐边/毛刺未去除','Casting'),
    ('A55','Casting Damage','铸件损坏','Casting'),
    ('A03','Failed Holiday Test','假日测试失败','Coating & Surface'),
    ('A13','Internal Coating < 350 microns','内涂层厚度不足350微米','Coating & Surface'),
    ('A14','External Coating < 300 microns','外涂层厚度不足300微米','Coating & Surface'),
    ('A15','Socket Gasket Fitment Issue (Excess Coating)','承口垫圈配合问题（涂层过厚）','Coating & Surface'),
    ('A16','Coating Not Adhered Properly','涂层附着不良','Coating & Surface'),
    ('A19','Bubble Wrap Impressions','气泡膜压痕','Coating & Surface'),
    ('A20','Coating Damage','涂层损坏','Coating & Surface'),
    ('A21','Scuff / Drag Marks / Dirt / Chalk','划痕/污迹/粉笔印','Coating & Surface'),
    ('A22','Surface Rust','表面锈蚀','Coating & Surface'),
    ('A23','Paint on Components','零部件沾漆','Coating & Surface'),
    ('A24','Paint Drip Marks','油漆滴流痕','Coating & Surface'),
    ('A25','Rework Not Completed','返工未完成','Coating & Surface'),
    ('A30','Surface Finish Not Passivated','表面未钝化处理','Coating & Surface'),
    ('A53','Coating Discrepancies','涂层不一致','Coating & Surface'),
    ('A57','Improper Coating Curing Prior to Packaging','包装前涂层固化不当','Coating & Surface'),
    ('A17','Bolt Holes Not to Specification','螺栓孔不符规格','Machining & Drilling'),
    ('A41','Flange Bolt Hole Not Drilled','法兰螺栓孔未钻','Machining & Drilling'),
    ('A43','Tapping Not to Specification','攻丝不符规格','Machining & Drilling'),
    ('A45','Flange Drilling Not to Specification','法兰钻孔不符规格','Machining & Drilling'),
    ('A54','Pre-tap Boss Not Machined Properly','预攻孔凸台加工不当','Machining & Drilling'),
    ('A56','Incorrect Dimensions','尺寸不正确','Machining & Drilling'),
    ('A31','Instructions Missing','说明书缺失','Marking & Packaging'),
    ('A32','Markings Not to Specification','标识不符规格','Marking & Packaging'),
    ('A33','Compliance Sticker Missing','合规贴纸缺失','Marking & Packaging'),
    ('A34','Label Sticker Missing','标签贴纸缺失','Marking & Packaging'),
    ('A35','Broken Crates','木箱破损','Marking & Packaging'),
    ('A36','Missing Packaging Material','包装材料缺失','Marking & Packaging'),
    ('A37','Screws/Nails Sticking into Crate','螺钉/钉子突出木箱','Marking & Packaging'),
    ('A38','Products Packed Not to Specification','装箱方式不符规格','Marking & Packaging'),
    ('A39','Crate Labelling Incorrect','木箱标签错误','Marking & Packaging'),
    ('A40','Crate Water Ingress','木箱进水','Marking & Packaging'),
    ('A18','Non-Conforming Material','不合格材料','Components & Materials'),
    ('A46','Grip Ring Split Gap Too Short','止退环分口间隙过小','Components & Materials'),
    ('A47','Grip Ring Teeth Missing','止退环齿缺失','Components & Materials'),
    ('A48','Gasket Damage','垫圈损坏','Components & Materials'),
    ('A50','Poor Welding','焊接质量差','Components & Materials'),
]

@contextmanager
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# (code, short_name, category_name, full_description)
_KB_ARTICLES = [
    ('A01','Assembly Issues','Assembly','Loose valve stems and caps; missing or misplaced bolts (retaining bolt on lid instead of frame); valve slider stuck in bore; bent lifting rings, damaged brass tapped holes, broken cap flange screws.'),
    ('A02','Missing Parts','Assembly','Units shipped missing lids, bonnet protectors, brass inserts, retaining bolts, washers, rubber gaskets, grub screws, keyhole plugs, and lid lifter hole caps.'),
    ('A03','Failed Holiday Test','Coating & Surface','Holiday test failures primarily inside the bore, at spigot edges, and flange inner edges. Presence of casting spikes and sharp casting poking through the coating.'),
    ('A04','Hole in Casting','Casting','Recurrence of pinholes, shrinkage holes, and sand holes. Big holes and casting shrinkage inside the bore and socket areas. Shrinking issues across the gasket area.'),
    ('A05','Socket Profile Cast Cavity','Casting','Casting issues and excess casting within the socket and gasket areas. Inability to properly fit gaskets into the socket profiles. Poor casting combined with coating defects in the gasket seat area.'),
    ('A06','Cast Deformity','Casting','Casting spikes and sharp edges protruding through coating on flange inner edges and bore. Bent spindle and poor spindle key fitment compromising torque. Thin frame walls; excess casting blocking bolt holes and access holes.'),
    ('A07','Lumpy Casting','Casting','Excessive coating and lumps in bolt holes preventing installation of bolts and white hat. Lumps of excess paint and coating inside the bore. Paint and coating lumps around flange openings and spigots.'),
    ('A08','Flange Faces Not Parallel','Casting','Flange face not horizontal; angled at 0.8°. Non-parallel flanges prevent proper sealing.'),
    ('A09','Pinholes on Rough Surface','Casting','Pinholes found both inside and outside the bore. The internal bore exhibits lumpy casting and rough coating surface.'),
    ('A10','Socket Gasket Fitment Issue (Excess Casting)','Casting','Excess casting material in the gasket area prevents the primary seal from seating flat against the FBE coated surface. Casting irregularities within the socket profile cause direct interference with gasket fitment.'),
    ('A11','Pinholes on Smooth Surface','Casting','Pinholes within the internal bore deep enough to break the electrical insulation of the protective coating.'),
    ('A12','Raised Face Not Flat','Casting','Raised, wavy, or lumpy flange faces preventing proper sealing. Excessive and uneven coating buildup on flange faces and inner edges. Heavy grinding marks and poor rework damaging the original flange surface.'),
    ('A13','Internal Coating < 350 microns','Coating & Surface','Internal coating falling below the minimum 350-micron threshold. Very low readings as low as 25–80 microns, providing virtually no corrosion protection. Grinding marks and poor rework are the root cause for many low-coating spots.'),
    ('A14','External Coating < 300 microns','Coating & Surface','Failed to meet the minimum 300-micron requirement. Many readings between 90–240 microns. Low coating on valve bonnets, flange faces, end rings, and valve caps. Grinding marks identified as root cause.'),
    ('A15','Socket Gasket Fitment Issue (Excess Coating)','Coating & Surface','Excessive coating buildup inside socket profiles prevents rubber gaskets from seating correctly (gasket sticking out), leading to a high risk of leakage. Excess buildup on one side, likely caused by improper hanging during coating.'),
    ('A16','Coating Not Adhered Properly','Coating & Surface','Coating flaking, peeling, and failing to bond to surfaces, particularly on flange faces and around pre-tap holes. Exposed metal on frames, lids, and tapping areas.'),
    ('A17','Bolt Holes Not to Specification','Machining & Drilling','Bolt holes drilled off-centre and too close to the outside edge of the flange. Excess coating buildup inside and around bolt holes prevents insertion of bolts and top hat components.'),
    ('A18','Non-Conforming Material','Components & Materials','20mm pretap bushes for DN200 Quad cracking when drilled. Old Design bush delivered instead of New Design. Lumpy and very uneven gaskets. Deep scratches inside the UMC barrel.'),
    ('A19','Bubble Wrap Impressions','Coating & Surface','Affected items were packed before the coating was dry, causing bubble wrap to permanently indent the finish. Majority of defects on flange faces.'),
    ('A20','Coating Damage','Coating & Surface','Chipped and cracked coating caused by poor securement; products moving around in oversized crates. Chipping on flange edges, inner flanges, end rings, socket areas, spigot edges, and external surfaces.'),
    ('A21','Scuff / Drag Marks / Dirt / Chalk','Coating & Surface','Unknown foreign material stuck or flashing in the coating. Dried glue-like scuffs, large unknown foreign objects inside the bore. Metal dust inside frames that looks like rust.'),
    ('A22','Surface Rust','Coating & Surface','Large-scale water ingress into crates during transit causing widespread rust. Heavy surface rust on lid covers, frames, and base plates.'),
    ('A23','Paint on Components','Coating & Surface','Large paint drips on flange faces creating uneven surfaces. Paint on the threads of tapped bolt holes.'),
    ('A24','Paint Drip Marks','Coating & Surface','Paint drip marks on the flange face causing surface irregularities.'),
    ('A25','Rework Not Completed','Coating & Surface','Grinding marks left in the internal bore, on flange faces, socket profiles, spigot areas, and around bolt holes. Flange faces sanded excessively, leading to non-parallel or uneven surfaces.'),
    ('A29','Flashing Not Removed','Casting','Flashing not removed from the casting surfaces, leaving sharp protrusions.'),
    ('A30','Surface Finish Not Passivated','Coating & Surface','Deep scratches inside the barrel and bore leaving stainless steel vulnerable to immediate localised corrosion. Pitting marks inside the barrel.'),
    ('A31','Instructions Missing','Marking & Packaging','Incorrect instruction booklet for PE Fittings. Wrong version of instruction manual included in the unit.'),
    ('A32','Markings Not to Specification','Marking & Packaging','Product labels and stickers do not match the physical product. Missing letters in DAEMCO branding. Missing MMYY production markings. Incorrect product codes (e.g. code ending in S instead of required L).'),
    ('A33','Compliance Sticker Missing','Marking & Packaging','Missing QR Code sticker. Missing compliance sticker required for regulatory purposes.'),
    ('A34','Label Sticker Missing','Marking & Packaging','Missing water mark certification sticker. Missing product label sticker.'),
    ('A35','Broken Crates','Marking & Packaging','Crate brackets burst open, leaving the load secured only by a single steel strap. Damaged crates resulting from stacking of heavy crates.'),
    ('A36','Missing Packaging Material','Marking & Packaging','Missing bonnet protector and other required packaging materials.'),
    ('A37','Screws/Nails Sticking into Crate','Marking & Packaging','Brackets secured with nails instead of required screws. Nails protruding into the crate, creating sharp hazard points.'),
    ('A38','Products Packed Not to Specification','Marking & Packaging','Units poorly packed with no separation material, causing direct metal-on-metal contact and coating chips. Items in oversized crates allowing violent movement during transit. Overlapping flanges in wrong orientation.'),
    ('A39','Crate Labelling Incorrect','Marking & Packaging','Crates marked as Spigot contained Flange products and vice versa. Pre-tap components packed into wrong crate size designations. Crate labels not matching actual inventory inside.'),
    ('A40','Crate Water Ingress','Marking & Packaging','Water ingress during transportation or storage causing damage to products.'),
    ('A41','Flange Bolt Hole Not Drilled','Machining & Drilling','Flange bolt hole completely not drilled, preventing any assembly.'),
    ('A42','Spigot Not to Specification','Casting','Spigot angle failing to meet specifications (approximately 11°). Spigots too thin at the top. Lumps at the spigot edge and excess coating.'),
    ('A43','Tapping Not to Specification','Machining & Drilling','Inability to screw in grub screws, bolts, and bushes fully due to FBE coating buildup or too-tight tapping. Pretap holes not drilled at all. Threads not properly tapped in DN225, DN200, and DN100 pretaps.'),
    ('A45','Flange Drilling Not to Specification','Machining & Drilling','Off-centre PCDs leading to bolt holes in incorrect locations. Bolt holes drilled into the flange face or inner flange face. Holes too close to outside flange edge (1 mm gap). Total failure to drill mandatory bolt holes in some units.'),
    ('A46','Grip Ring Split Gap Too Short','Components & Materials','Grip ring split gap too short, preventing proper installation and function.'),
    ('A47','Grip Ring Teeth Missing','Components & Materials','Grip ring teeth missing, compromising the grip ring performance and pipe retention.'),
    ('A48','Gasket Damage','Components & Materials','Split, cut, and damaged gaskets. Pinched gasket. Moulding damage preventing proper sealing.'),
    ('A49','Sharp Edges / Edges Not Deburred','Casting','Rough surface on brass grip ring with exposed base material. Sharp edges in lid finger tabs creating safety hazard.'),
    ('A50','Poor Welding','Components & Materials','Poor welding both on external and internal seam of the barrel, compromising structural integrity.'),
    ('A51','Lid/Frame Assembly Not to Specification','Assembly','Failure to meet the 2.5mm flushness specification; lids sitting above the frame creating a trip hazard. Lateral movement of lids creating gaps exceeding 7.5mm. L-brackets, screws, and silicone completely detached.'),
    ('A52','Failed Pressure Test / Leaking Product','Assembly','Product fails pressure test. Leaking detected during inspection. Critical defect requiring rejection.'),
    ('A53','Coating Discrepancies','Coating & Surface','Extra metal material, lumps of coating, and unknown foreign material inside the bore. Areas with no bitumen coverage. Ripple patterns on fittings sides. Air bubbles in bore coating. Excess coating on spigot edges.'),
    ('A54','Pre-tap Boss Not Machined Properly','Machining & Drilling','Pre-tap bosses not machined to correct tolerances, causing O-rings to come out during drilling or assembly process.'),
    ('A55','Casting Damage','Casting','Casting damage (chips on lid and frame) that occurred before bitumen was applied. Internal cast for lid support not level, leading to rocking movement. Thin thickness on side of frame.'),
    ('A56','Incorrect Dimensions','Machining & Drilling','UMC gasket internal diameter out of specification, preventing the pipe from passing through. Dimensional non-conformance across critical measurement points.'),
    ('A57','Improper Coating Curing Prior to Packaging','Coating & Surface','Cardboard found stuck to flange faces because the bitumen coating had not fully dried before items were packed into the crate.'),
]

_KB_QUESTIONS = [
    ('Assembly', 'Which defect code covers loose valve stems, misplaced bolts, and stuck valve sliders?',
     'A01','A02','A03','A04','A','A01 — Assembly Issues covers all types of assembly defects.','Easy'),
    ('Assembly', 'Defect A52 refers to which critical failure?',
     'Surface rust','Failed pressure test / leaking product','Missing compliance sticker','Flange faces not parallel',
     'B','A52 is always a critical defect requiring rejection.','Easy'),
    ('Casting', 'What is defect code A08?',
     'Pinholes on rough surface','Lumpy casting','Flange faces not parallel','Hole in casting',
     'C','A08 covers flange faces that are not parallel, which prevents proper sealing.','Easy'),
    ('Coating & Surface', 'What is the minimum required INTERNAL coating thickness?',
     '100 microns','200 microns','300 microns','350 microns',
     'D','Per A13, internal coating must be at least 350 microns.','Medium'),
    ('Coating & Surface', 'What is the minimum required EXTERNAL coating thickness?',
     '150 microns','200 microns','300 microns','400 microns',
     'C','Per A14, external coating must be at least 300 microns.','Medium'),
    ('Coating & Surface', 'A product was packed before the coating dried, leaving bubble wrap impressions. Which code applies?',
     'A16','A19','A20','A21',
     'B','A19 — Bubble Wrap Impressions covers damage from premature packaging.','Medium'),
    ('Coating & Surface', 'A product arrives with surface rust from water entering the crate during transit. Which defect code?',
     'A20','A21','A22','A25',
     'C','A22 — Surface Rust covers water ingress during transit causing rust.','Easy'),
    ('Machining & Drilling', 'Which defect code covers off-centre flange drilling and bolt holes too close to the outside edge?',
     'A17','A41','A43','A45',
     'D','A45 — Flange Drilling Not to Specification covers PCD errors and off-centre holes.','Medium'),
    ('Machining & Drilling', 'An inspector finds that threads cannot be tapped properly and grub screws cannot be fully inserted. Which code?',
     'A06','A17','A43','A45',
     'C','A43 — Tapping Not to Specification.','Easy'),
    ('Marking & Packaging', 'Products are found inside a crate labelled for a different product. Which defect applies?',
     'A32','A33','A38','A39',
     'D','A39 — Crate Labelling Incorrect covers mismatched crate labels and contents.','Medium'),
    ('Marking & Packaging', 'Which defect code covers products packed in oversized crates with no separation material?',
     'A35','A36','A38','A40',
     'C','A38 — Products Packed Not to Specification covers poor packing practices.','Easy'),
    ('Components & Materials', 'An inspector finds gaskets that are split and show moulding damage. Which code?',
     'A46','A47','A48','A50',
     'C','A48 — Gasket Damage covers split, cut, pinched, and moulding-damaged gaskets.','Easy'),
]


_DAEMCO_PRODUCT_CATS = [
    ('Gate Valves (RSV)',      'Daemco Resilient Seat Gate Valves for civil water infrastructure'),
    ('Ductile Iron Systems',   'Daemco Ductile Iron Pipe Fittings and Blank Flanges'),
    ('PE Products',            'Daemco PE couplings, flange adaptors, gate valves and tapping bands'),
    ('Clamps & Couplings',     'Daemco mechanical couplings and tapping bands'),
    ('Streetware',             'Daemco valve and hydrant covers and lids'),
    ('Accessories & Spindles', 'Daemco extension spindles and handwheels'),
]

_DAEMCO_PRODUCT_ARTICLES = [
    ('Gate Valves (RSV)', 'Gate Valves (RSV) — Product Overview',
     '''Daemco Resilient Seat Gate Valves (RSV) are used in civil water infrastructure pipelines.

TYPES & SPECIFICATIONS
• Flange RSG Valve:  DN50, DN80–DN375, PN16
• Spigot RSG Valve:  DN100, DN150, PN16
• Socket RSG Valve:  DN100, DN150, PN16
• PE RSG Valve:      DN125, DN180, PN16

CLOSING DIRECTION OPTIONS
• ACC — Anti-Clockwise Close
• CC  — Clockwise Close
Both options are available across the range.

PRESSURE RATING
All types are rated to PN16 (16 bar working pressure).

INSPECTION CHECKLIST
1. Confirm DN size matches order specification.
2. Verify PN16 pressure rating marking on body.
3. Check closing direction (ACC vs CC) matches purchase order.
4. Inspect resilient seat for cuts, deformation, or misalignment.
5. Operate valve — check movement is smooth and full travel is achievable.
6. Inspect bolting, flange faces, and coatings for damage.''',
     'RSV gate valve PN16 flange spigot socket PE ACC CC'),

    ('Ductile Iron Systems', 'Ductile Iron Pipe Systems — Product Overview',
     '''Daemco Ductile Iron Pipe Fittings and Flanges for civil pipeline infrastructure.

SPECIFICATIONS
• Size Range:     DN50 to DN375
• Pressure Rating: PN16
• Connections:    Flange, Socket, Spigot, and Blank End

PRODUCTS
• Ductile Iron Pipe Fittings — bends, tees, reducers (DN50–DN375, PN16)
• Blank Flanges — standard and tapped versions (DN50–DN375, PN16)

MATERIALS & COATINGS
• Material: Ductile iron (higher tensile strength than cast iron)
• Typical internal coating: epoxy or cement mortar lining
• Typical external coating: bitumen or epoxy

INSPECTION CHECKLIST
1. Verify DN size and PN16 marking on fitting body.
2. Confirm connection type (flange/socket/spigot) matches specification.
3. Inspect casting surface for cracks, cold shuts, or porosity.
4. Check coating thickness and integrity — no bare metal, blistering, or peeling.
5. Verify flange faces are flat and parallel (use straight edge if in doubt).
6. For tapped blank flanges: check thread quality and verify tap size.''',
     'ductile iron DI fitting flange socket spigot blank PN16'),

    ('PE Products', 'PE Products — Product Overview',
     '''Daemco PE Products are designed for polyethylene pipe systems in water infrastructure.

PRODUCTS & SPECIFICATIONS
• PE Couplings:            DN125 and DN180, PN16
• PE Flange Adaptors:      DN125 and DN180, PN16
• PE Resilient Seat Gate Valves: DN125 and DN180, PN16
• PE Tapping Bands:        DN63 to DN315, PN16

KEY TECHNOLOGY
All PE coupling products use Rubber Ring Joint Restraint (RRJR) connection technology.
This provides a secure, flexible joint without welding or electrofusion.

COLOUR CODE
Blue fittings = potable water systems.
Purple fittings = recycled / non-potable water systems.

INSPECTION CHECKLIST
1. Confirm DN size matches specification (common sizes: DN63, DN125, DN180, DN315).
2. Check rubber ring seals — no cuts, distortion, ageing cracks, or contamination.
3. Verify PE material markings and pressure class (PN16).
4. For tapping bands: check saddle profile matches pipe OD; no cracking in PE body.
5. For gate valves: operate stem and verify smooth full travel open/close.
6. Confirm colour coding matches intended water type (blue vs purple).''',
     'PE polyethylene coupling flange adaptor gate valve tapping band RRJR PN16'),

    ('Clamps & Couplings', 'Clamps & Couplings — Product Overview',
     '''Daemco Clamps and Couplings for mechanical pipe connections in civil works.

PRODUCTS & SPECIFICATIONS
• Unrestrained Mechanical Couplings: DN80–DN600, PN16
• Series 1 Tapping Bands:  DN40–DN300, PN16
• Series 2 Tapping Bands:  DN100–DN300, PN16 (blue and purple)
• PE Tapping Bands:        DN63–DN315, PN16

MECHANICAL COUPLING FEATURES
• Unrestrained — allows limited angular deflection and pipe movement
• Suitable for joining plain-end pipes without welding
• Rubber gasket provides seal; bolted housing applies clamping force

TAPPING BAND APPLICATIONS
Allow branch connections to be made on live pressurised mains without full cut-in.

COLOUR CODING (Tapping Bands)
• Blue  = potable water
• Purple = recycled/non-potable water

INSPECTION CHECKLIST
1. Verify DN size matches pipe OD specification.
2. Check rubber gasket/seal condition — no cuts, swelling, or hardening.
3. Inspect all bolts and nuts — correct grade, no corrosion, washer fitted.
4. Confirm colour coding matches water type (blue/purple).
5. Verify PN16 pressure rating and product markings.
6. For tapping bands: check outlet thread size and protective plug is present.''',
     'coupling tapping band mechanical DN80 DN600 blue purple PN16'),

    ('Streetware', 'Streetware — Covers and Lids Overview',
     '''Daemco Streetware protects buried valve and hydrant assets with compliant covers and lids.

PRODUCT RANGE
Non-Trafficable:
• Valve and Hydrant Hinged Lids (colour-coded keys)
• Lamp Hole Covers

Trafficable (VIC):
• Key Lids and Drop-In Lids (colour-coded)

Trafficable (QLD):
• Valve and Hydrant Box Covers
• Drop-In Lids

Trafficable (NSW):
• Concrete Covers and Lids
• Asphalt Covers and Lids

Specialty:
• L-Type Covers — hydrants and air valves
• 199 and 200 Covers — standard variants
• GFC Syphon Boxes — trafficable installation
• Shroud Base Plates — DN225 shroud pipe
• GWW ACC Lids — Greater Western Water (Melton Area)
• Curb Adaptors — 100mm pipe

INSPECTION CHECKLIST
1. Confirm trafficable vs non-trafficable rating matches site specification.
2. Verify state/region compliance (VIC, QLD, or NSW product variant).
3. Check load rating marking is present and legible.
4. Inspect hinge, lock, and key operation — all should function without binding.
5. Check lid sits flush in frame — no rocking or excessive gap.
6. Verify colour identification code matches valve/hydrant type at that location.''',
     'streetware cover lid trafficable hydrant valve VIC QLD NSW'),

    ('Accessories & Spindles', 'Accessories & Extension Spindles — Product Overview',
     '''Daemco Accessories complete the valve and fitting product range.

PRODUCTS
• Valve Extension Spindles: 150mm to 1500mm lengths
  — Compatible with all valve types, DN50 to DN375
• Handwheels: compatible with DN50 to DN375 valves

EXTENSION SPINDLE APPLICATIONS
Used when a valve is installed at depth requiring surface-level operation.
Allows key or handwheel operation without excavating.
Typically installed inside a valve extension box (Streetware product).

SPINDLE SIZING
Length = installation depth of valve stem below finished surface level.
Available from 150mm up to 1500mm (select to nearest available increment).

INSPECTION CHECKLIST
1. Verify spindle length matches installation depth on drawings.
2. Check stem connection profile — square or pentagon key, correct size.
3. Inspect entire spindle length for bends (causes binding on operation).
4. Verify material: stainless steel or hot-dip galvanised (no bare carbon steel).
5. Handwheels: confirm fixing bolt is tight and key profile matches valve stem.
6. Check protective plastic cap on top of spindle is present.''',
     'extension spindle handwheel valve accessories DN50 DN375'),
]

_DAEMCO_PRODUCT_QUESTIONS = [
    # Gate Valves
    ('Gate Valves (RSV)',
     'What pressure rating do all Daemco Resilient Seat Gate Valves carry?',
     'PN10', 'PN16', 'PN25', 'PN6',
     'B', 'All Daemco RSV gate valves are rated PN16 (16 bar working pressure).', 'Easy'),
    ('Gate Valves (RSV)',
     'Which DN size range is available for the Flange Resilient Seat Gate Valve?',
     'DN25–DN200', 'DN50–DN600', 'DN50–DN375', 'DN80–DN300',
     'C', 'Flange RSG Valve is available DN50 and DN80 through DN375.', 'Easy'),
    ('Gate Valves (RSV)',
     'What does "ACC" stand for in Daemco gate valve closing direction?',
     'Automatic Closing Control', 'Anti-Clockwise Close', 'Actuated Cam Control', 'Axial Coupling Connection',
     'B', 'ACC = Anti-Clockwise Close. CC = Clockwise Close. Always verify which is required per PO.', 'Medium'),
    # Ductile Iron
    ('Ductile Iron Systems',
     'Which connection types are available for Daemco Ductile Iron Pipe Fittings?',
     'Flange only', 'Flange, Socket, Spigot', 'Socket and PE only', 'Compression and Push-fit',
     'B', 'DI fittings come in Flange, Socket, Spigot, and Blank End connections.', 'Easy'),
    ('Ductile Iron Systems',
     'Which of these is a variant of Daemco Blank Flanges?',
     'Grooved', 'Tapped', 'Push-fit', 'Compression',
     'B', 'Blank Flanges are available in standard and tapped versions.', 'Easy'),
    # PE Products
    ('PE Products',
     'What connection technology do Daemco PE coupling products use?',
     'Electrofusion', 'Butt fusion', 'Rubber Ring Joint Restraint', 'Compression only',
     'C', 'All PE coupling products use Rubber Ring Joint Restraint (RRJR) connection technology.', 'Easy'),
    ('PE Products',
     'What DN size range do Daemco PE Tapping Bands cover?',
     'DN50–DN200', 'DN63–DN315', 'DN80–DN375', 'DN100–DN400',
     'B', 'PE Tapping Bands cover DN63 to DN315.', 'Medium'),
    # Clamps
    ('Clamps & Couplings',
     'What is the maximum DN size for Daemco Unrestrained Mechanical Couplings?',
     'DN375', 'DN400', 'DN500', 'DN600',
     'D', 'Unrestrained Mechanical Couplings are available up to DN600.', 'Medium'),
    ('Clamps & Couplings',
     'Series 2 Tapping Bands are available in which two colours?',
     'Red and Green', 'Blue and Purple', 'Yellow and Orange', 'Black and White',
     'B', 'Blue = potable water; Purple = recycled/non-potable water.', 'Easy'),
    # Streetware
    ('Streetware',
     'Which Daemco lid product is specified for Greater Western Water (Melton Area)?',
     '199 Covers', 'L-Type Covers', 'GWW ACC Lids', 'Lamp Hole Covers',
     'C', 'GWW ACC Lids are specifically designed for Greater Western Water installations in the Melton Area.', 'Hard'),
    ('Streetware',
     'L-Type Covers are designed to be used with which types of buried assets?',
     'Gate valves only', 'Hydrants and Air Valves', 'Syphon boxes only', 'Tapping bands',
     'B', 'L-Type Covers are suitable for Hydrants and Air Valves.', 'Medium'),
    # Accessories
    ('Accessories & Spindles',
     'What is the maximum length of Daemco Valve Extension Spindles?',
     '500mm', '1000mm', '1200mm', '1500mm',
     'D', 'Extension spindles are available from 150mm up to 1500mm.', 'Easy'),
    ('Accessories & Spindles',
     'Extension spindles are compatible with valve DN sizes ranging from:',
     'DN50–DN200', 'DN50–DN375', 'DN80–DN600', 'DN100–DN375',
     'B', 'Extension spindles are compatible with all valve types from DN50 to DN375.', 'Easy'),
]


def init_db():
    with db_conn() as conn:
        conn.executescript(_SCHEMA)

        # ── Column migrations ──────────────────────────────────────────────
        for col, defn in [('gender', "TEXT DEFAULT ''"),
                           ('mobile', "TEXT DEFAULT ''"),
                           ('department', "TEXT DEFAULT ''"),
                           ('is_stationed', 'INTEGER DEFAULT 0')]:
            try:
                conn.execute(f'ALTER TABLE employees ADD COLUMN {col} {defn}')
            except Exception:
                pass

        for col, defn in [
            ('checkin_lat',   'REAL DEFAULT NULL'),
            ('checkin_lng',   'REAL DEFAULT NULL'),
            ('checkout_lat',  'REAL DEFAULT NULL'),
            ('checkout_lng',  'REAL DEFAULT NULL'),
            ('gps_verified',  'INTEGER DEFAULT 0'),
        ]:
            try:
                conn.execute(f'ALTER TABLE attendance_records ADD COLUMN {col} {defn}')
            except Exception:
                pass

        for col, defn in [('sub_category', "TEXT DEFAULT ''"),
                           ('crate_qty', 'INTEGER DEFAULT NULL'),
                           ('inspect_pcs', "TEXT DEFAULT ''"),
                           ('inspect_mins', 'REAL DEFAULT NULL')]:
            try:
                conn.execute(f'ALTER TABLE products ADD COLUMN {col} {defn}')
            except Exception:
                pass

        # Defect codes
        existing_dc = {r[0] for r in conn.execute('SELECT code FROM defect_codes').fetchall()}
        for row in _DEFECT_SEEDS:
            if row[0] not in existing_dc:
                conn.execute('INSERT INTO defect_codes (code,name,name_cn,category) VALUES (?,?,?,?)', row)

        # KB categories (only seed if empty)
        if conn.execute('SELECT COUNT(*) FROM kb_categories').fetchone()[0] == 0:
            cat_names = ['Assembly','Casting','Coating & Surface','Machining & Drilling','Marking & Packaging','Components & Materials']
            for name in cat_names:
                conn.execute('INSERT INTO kb_categories (name) VALUES (?)', (name,))

        # KB articles (only seed if empty)
        if conn.execute('SELECT COUNT(*) FROM kb_articles').fetchone()[0] == 0:
            cat_map = {r[1]: r[0] for r in conn.execute('SELECT id, name FROM kb_categories').fetchall()}
            for code, short_name, cat_name, content in _KB_ARTICLES:
                cat_id = cat_map.get(cat_name)
                title = f'{code} — {short_name}'
                conn.execute(
                    'INSERT INTO kb_articles (category_id, title, content, tags) VALUES (?,?,?,?)',
                    (cat_id, title, content, code))

        # Questions (only seed if empty)
        if conn.execute('SELECT COUNT(*) FROM questions').fetchone()[0] == 0:
            cat_map = {r[1]: r[0] for r in conn.execute('SELECT id, name FROM kb_categories').fetchall()}
            for cat_name, q, a, b, c, d, ans, expl, diff in _KB_QUESTIONS:
                cat_id = cat_map.get(cat_name)
                conn.execute(
                    'INSERT INTO questions (category_id,question,option_a,option_b,option_c,option_d,answer,explanation,difficulty) VALUES (?,?,?,?,?,?,?,?,?)',
                    (cat_id, q, a, b, c, d, ans, expl, diff))

        # Daemco product knowledge categories & articles (seed by name — safe to run multiple times)
        existing_cats = {r[0] for r in conn.execute('SELECT name FROM kb_categories').fetchall()}
        for cat_name, cat_desc in _DAEMCO_PRODUCT_CATS:
            if cat_name not in existing_cats:
                conn.execute('INSERT INTO kb_categories (name, description) VALUES (?,?)', (cat_name, cat_desc))

        cat_map2 = {r[1]: r[0] for r in conn.execute('SELECT id, name FROM kb_categories').fetchall()}
        existing_articles = {r[0] for r in conn.execute('SELECT title FROM kb_articles').fetchall()}
        for cat_name, title, content, tags in _DAEMCO_PRODUCT_ARTICLES:
            if title not in existing_articles:
                cat_id = cat_map2.get(cat_name)
                conn.execute('INSERT INTO kb_articles (category_id,title,content,tags) VALUES (?,?,?,?)',
                             (cat_id, title, content, tags))

        existing_q = {r[0] for r in conn.execute('SELECT question FROM questions').fetchall()}
        cat_map3 = {r[1]: r[0] for r in conn.execute('SELECT id, name FROM kb_categories').fetchall()}
        for cat_name, q, a, b, c, d, ans, expl, diff in _DAEMCO_PRODUCT_QUESTIONS:
            if q not in existing_q:
                cat_id = cat_map3.get(cat_name)
                conn.execute(
                    'INSERT INTO questions (category_id,question,option_a,option_b,option_c,option_d,answer,explanation,difficulty) VALUES (?,?,?,?,?,?,?,?,?)',
                    (cat_id, q, a, b, c, d, ans, expl, diff))
