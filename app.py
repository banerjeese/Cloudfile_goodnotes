"""PDF ink editor: browse PDFs in a server folder, draw on them with a stylus, save in place.
Run:  pip install -r requirements.txt
      APP_PASSWORD=secret ROOT=~/remote_storage python app.py
"""
import os, re, shutil, tempfile, base64, colorsys
from urllib.parse import quote
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, send_file, send_from_directory, abort, Response

ROOT = os.path.realpath(os.path.expanduser(os.environ.get("ROOT", "~/remote_storage")))
USER = os.environ.get("APP_USER", "admin")
PASSWORD = os.environ.get("APP_PASSWORD", "")
PORT = int(os.environ.get("PORT", "8090"))
app = Flask(__name__, static_folder="static", static_url_path="/static")

DARK = "pdfink-dark"          # subject of the per-page dark layer
LC = "pdfink-lc="             # subject prefix on ink: the colour you picked (the annotation itself holds the colour as displayed)

@app.before_request
def guard():
    if not PASSWORD:
        return
    a = request.authorization
    if not a or a.username != USER or a.password != PASSWORD:
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="PDF Ink"'})

def safe(rel):
    p = os.path.realpath(os.path.join(ROOT, (rel or "").lstrip("/")))
    if os.path.commonpath([p, ROOT]) != ROOT:
        abort(403)
    return p

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/list")
def api_list():
    d = safe(request.args.get("path", ""))
    if not os.path.isdir(d):
        abort(404)
    items = []
    for n in sorted(os.listdir(d), key=str.lower):
        if n.startswith("."):
            continue
        full = os.path.join(d, n)
        rel = os.path.relpath(full, ROOT)
        if os.path.isdir(full):
            items.append({"name": n, "path": rel, "dir": True})
        elif n.lower().endswith(".pdf"):
            items.append({"name": n, "path": rel, "dir": False})
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return jsonify(items)

@app.route("/api/file")
def api_file():
    p = safe(request.args.get("path", ""))
    if not p.lower().endswith(".pdf") or not os.path.isfile(p):
        abort(404)
    r = send_file(p, mimetype="application/pdf")
    r.headers["Cache-Control"] = "no-store"
    return r

# ---------------------------------------------------------------- download (everything merged into the pages, still vector)
@app.route("/api/download")
def api_download():
    p = safe(request.args.get("path", ""))
    if not p.lower().endswith(".pdf") or not os.path.isfile(p):
        abort(404)
    name = os.path.basename(p)
    doc = fitz.open(p)
    try:
        if request.args.get("mode", "flat") != "edit":
            try:
                doc.bake(annots=True, widgets=True)      # burn ink / text / pictures / dark layers into the page content
            except Exception:
                pass                                     # older PyMuPDF: annotations stay as they are (still shown by viewers)
        data = doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()
    ascii_name = name.encode("ascii", "ignore").decode() or "document.pdf"
    r = Response(data, mimetype="application/pdf")
    r.headers["Content-Disposition"] = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name.replace('"', ""), quote(name))
    r.headers["Cache-Control"] = "no-store"
    return r

# ---------------------------------------------------------------- rename
@app.route("/api/rename", methods=["POST"])
def api_rename():
    b = request.get_json(force=True) or {}
    p = safe(b.get("path", ""))
    if not p.lower().endswith(".pdf") or not os.path.isfile(p):
        abort(404)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(b.get("name", "")).strip()).strip(". ")
    if name.lower().endswith(".pdf"):
        name = name[:-4].strip(". ")
    name = name[:100]
    if not name:
        return jsonify(error="Name can't be empty"), 400
    new = os.path.join(os.path.dirname(p), name + ".pdf")
    if new == p:
        return jsonify(ok=True, path=os.path.relpath(p, ROOT))
    if os.path.exists(new):
        return jsonify(error="A file with that name already exists"), 409
    os.rename(p, new)
    bak = p + ".orig.bak"
    if os.path.exists(bak) and not os.path.exists(new + ".orig.bak"):
        os.rename(bak, new + ".orig.bak")
    return jsonify(ok=True, path=os.path.relpath(new, ROOT))

# ---------------------------------------------------------------- read ink / text / pictures / dark pages
@app.route("/api/strokes")
def api_strokes():
    """Existing ink + text + pictures in the PDF, as editable items (normalised 0-1 coords), plus the list of dark pages."""
    p = safe(request.args.get("path", ""))
    if not p.lower().endswith(".pdf") or not os.path.isfile(p):
        abort(404)
    out, dark = [], []
    doc = fitz.open(p)
    for pi, pg in enumerate(doc):
        r = pg.rect
        for an in pg.annots(types=[fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_FREE_TEXT, fitz.PDF_ANNOT_STAMP]) or []:
            t = an.type[0]
            sub0 = (an.info or {}).get("subject") or ""
            if t == fitz.PDF_ANNOT_STAMP:
                if sub0 == DARK:
                    if pi not in dark:
                        dark.append(pi)
                    continue
                if not sub0.startswith("pdfink-img"):
                    continue
                q = (fitz.Rect(an.rect) * pg.rotation_matrix).normalize()
                out.append({"kind": "img", "page": pi, "xref": an.xref,
                            "x": q.x0 / r.width, "y": q.y0 / r.height,
                            "wd": q.width / r.width, "ht": q.height / r.height})
                continue
            if t == fitz.PDF_ANNOT_FREE_TEXT:
                info = an.info or {}
                txt = info.get("content", "") or ""
                if not txt.strip():
                    continue
                sub = info.get("subject", "") or ""
                m = re.search(r"pdfink:size=([0-9.]+);color=(#[0-9a-fA-F]{6})", sub)
                fm = re.search(r";font=(helv|tiro|cour)", sub)
                fb, fi, fu = (re.search(r";%s=([01])" % k, sub) for k in "biu")
                size = float(m.group(1)) if m else 12.0 / r.width
                color = m.group(2) if m else "#111111"
                q = (fitz.Rect(an.rect) * pg.rotation_matrix).normalize()
                out.append({"kind": "text", "page": pi, "xref": an.xref, "color": color, "size": size,
                            "x": q.x0 / r.width, "y": q.y0 / r.height, "wd": q.width / r.width, "text": txt,
                            "font": fm.group(1) if fm else "helv",
                            "b": int(fb.group(1)) if fb else 0,
                            "i": int(fi.group(1)) if fi else 0,
                            "ul": int(fu.group(1)) if fu else 0})
                continue
            ml = re.match(r"pdfink-lc=(#[0-9a-fA-F]{6})", sub0)
            if ml:
                color = ml.group(1).lower()                 # the colour you picked
            else:
                st = (an.colors or {}).get("stroke") or (0, 0, 0)
                color = "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in st[:3])
            op = an.opacity
            w = (an.border or {}).get("width", 1) / r.width
            for sub in an.vertices or []:
                pts = []
                for x, y in sub:
                    q = fitz.Point(x, y) * pg.rotation_matrix
                    pts.append([q.x / r.width, q.y / r.height])
                if len(pts) > 1:
                    out.append({"page": pi, "xref": an.xref, "color": color, "w": w,
                                "a": op if op is not None and op >= 0 else 1, "pts": pts})
    doc.close()
    return jsonify(m=os.path.getmtime(p), strokes=out, dark=dark)

@app.route("/api/img")
def api_img():
    p = safe(request.args.get("path", ""))
    if not p.lower().endswith(".pdf") or not os.path.isfile(p):
        abort(404)
    try:
        xref = int(request.args.get("xref", "0"))
    except ValueError:
        abort(400)
    doc = fitz.open(p)
    data = None
    try:
        for pg in doc:
            for an in pg.annots(types=[fitz.PDF_ANNOT_STAMP]) or []:
                if an.xref != xref:
                    continue
                rc = an.rect
                sc = min(8.0, 1600.0 / max(rc.width, rc.height, 1.0))
                try:
                    pix = an.get_pixmap(matrix=fitz.Matrix(sc, sc), alpha=True)
                except Exception:
                    pix = an.get_pixmap(matrix=fitz.Matrix(sc, sc))
                data = pix.tobytes("png")
                break
            if data:
                break
    finally:
        doc.close()
    if not data:
        abort(404)
    r = Response(data, mimetype="image/png")
    r.headers["Cache-Control"] = "no-store"
    return r

# ---------------------------------------------------------------- helpers
FONTMAP = {"helv": ("helv", "hebo", "heit", "hebi"),
           "tiro": ("tiro", "tibo", "tiit", "tibi"),
           "cour": ("cour", "cobo", "coit", "cobi")}

def pdf_font(f, b, i):
    return FONTMAP.get(f, FONTMAP["helv"])[(1 if b else 0) + (2 if i else 0)]

def wrap_lines(text, fs, width, fontname="helv"):
    out = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            t = (cur + " " + word) if cur else word
            try:
                ln = fitz.get_text_length(t, fontname=fontname, fontsize=fs)
            except Exception:
                ln = len(t) * fs * 0.5
            if cur and ln > width:
                out.append(cur)
                cur = word
            else:
                cur = t
        out.append(cur)
    return out

def backup_once(p):
    bak = p + ".orig.bak"
    if not os.path.exists(bak):
        shutil.copy2(p, bak)

def disp_color(hexc, dark):
    """Colour as it should appear on a dark page (same rule as the browser): dark colours become light, hue is kept."""
    if not dark:
        return hexc.lower()
    c = hexc.lstrip("#")
    r, g, b = (int(c[i:i+2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    L = min(0.92, max(0.62, 1 - l))
    r, g, b = colorsys.hls_to_rgb(h, L, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))

def rgb_t(hexc):
    c = hexc.lstrip("#")
    return tuple(int(c[i:i+2], 16) / 255 for i in (0, 2, 4))

def is_dark_page(pg):
    for an in pg.annots(types=[fitz.PDF_ANNOT_STAMP]) or []:
        if ((an.info or {}).get("subject") or "") == DARK:
            return True
    return False

def remove_dark(pg):
    for an in list(pg.annots(types=[fitz.PDF_ANNOT_STAMP]) or []):
        if ((an.info or {}).get("subject") or "") == DARK:
            pg.delete_annot(an)

def reload(doc, pg):
    try:
        return doc.reload_page(pg)
    except Exception:
        return doc[pg.number]

# ---------------------------------------------------------------- write items
def add_text(doc, s, dark=False):
    """Add one text box as a FreeText annotation. Returns its xref (or None)."""
    text = str(s.get("text", ""))[:5000]
    if not text.strip() or not re.fullmatch(r"#[0-9a-fA-F]{6}", s.get("color", "")):
        return None
    pg = doc[int(s["page"])]
    r = pg.rect
    size = float(s["size"])
    fs = max(4.0, size * r.width)
    x0, y0 = float(s["x"]) * r.width, float(s["y"]) * r.height
    wd = max(30.0, float(s["wd"]) * r.width)
    fkey = s.get("font", "helv")
    if fkey not in FONTMAP:
        fkey = "helv"
    fn = pdf_font(fkey, s.get("b"), s.get("i"))
    lines = wrap_lines(text, fs, wd, fn)
    h = (len(lines) + 1) * fs * 1.25 + fs
    rect = fitz.Rect(x0, y0, x0 + wd, y0 + h) * pg.derotation_matrix
    rgb = rgb_t(disp_color(s["color"], dark))                # shown colour
    a = pg.add_freetext_annot(rect.normalize(), text, fontsize=fs, fontname=fn,
                              text_color=rgb, fill_color=None, align=0)
    if not a:
        return None
    a.set_info(subject="pdfink:size=%.5f;color=%s;font=%s;b=%d;i=%d;u=%d" % (
        size, s["color"].lower(), fkey,                      # the subject keeps the colour you picked
        1 if s.get("b") else 0, 1 if s.get("i") else 0, 1 if s.get("u") else 0))
    xref = a.xref
    if s.get("u"):
        for k, ln in enumerate(lines):
            if not ln.strip():
                continue
            ww = fitz.get_text_length(ln, fontname=fn, fontsize=fs)
            yy = y0 + k * fs * 1.2 + fs * 1.05
            u = pg.add_line_annot(fitz.Point(x0, yy) * pg.derotation_matrix,
                                  fitz.Point(x0 + ww, yy) * pg.derotation_matrix)
            u.set_colors(stroke=rgb)
            u.set_border(width=max(0.5, fs * 0.06))
            u.set_info(subject="pdfink-ul:%d" % xref)
            u.update()
    return xref

def add_image(doc, s):
    """Picture as a Stamp annotation. Returns xref, -1 if merged into the page, None on failure."""
    m = re.match(r"data:image/(png|jpeg|jpg);base64,(.+)$", str(s.get("data", "")), re.S)
    if not m:
        return None
    raw = base64.b64decode(m.group(2))
    pg = doc[int(s["page"])]
    r = pg.rect
    x0, y0 = float(s["x"]) * r.width, float(s["y"]) * r.height
    rect = fitz.Rect(x0, y0, x0 + float(s["wd"]) * r.width, y0 + float(s["ht"]) * r.height)
    rect = (rect * pg.derotation_matrix).normalize()
    a = None
    try:
        pix = fitz.Pixmap(raw)
    except Exception:
        pix = None
    attempts = ([lambda: pg.add_stamp_annot(rect, stamp=pix)] if pix is not None else []) + \
               [lambda: pg.add_stamp_annot(rect, stamp=raw)]
    for fn in attempts:
        try:
            a = fn()
            if a:
                break
        except Exception:
            a = None
    if a:
        a.set_info(subject="pdfink-img")
        return a.xref
    pg.insert_image(rect, stream=raw, overlay=True)
    return -1

def add_ink(doc, s, dark=False):
    """One pen/highlighter stroke as an Ink annotation (stored in its shown colour; your colour goes in the subject)."""
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", s.get("color", "")) or len(s.get("pts", [])) < 2:
        return None
    pg = doc[int(s["page"])]
    r = pg.rect
    pts = []
    for x, y in s["pts"]:
        q = fitz.Point(x * r.width, y * r.height) * pg.derotation_matrix
        pts.append((float(q.x), float(q.y)))
    a = pg.add_ink_annot([pts])
    a.set_colors(stroke=rgb_t(disp_color(s["color"], dark)))
    a.set_border(width=max(0.3, float(s["w"]) * r.width))
    a.set_opacity(float(s.get("a", 1)))
    a.set_info(subject=LC + s["color"].lower())
    a.update()
    return a.xref

# ---------------------------------------------------------------- dark pages (saved inside the PDF)
def _inverted_ok(pg, before):
    """Check that the page now renders as the inverse of the untouched page."""
    try:
        now = pg.get_pixmap(dpi=24)
        if (now.width, now.height) != (before.width, before.height):
            return False
        good = 0
        for fx, fy in ((.03, .03), (.97, .03), (.03, .97), (.97, .97), (.5, .02), (.5, .98)):
            x, y = int(now.width * fx), int(now.height * fy)
            a, b = before.pixel(x, y), now.pixel(x, y)
            if all(abs((255 - a[k]) - b[k]) < 50 for k in range(3)):
                good += 1
        return good >= 5
    except Exception:
        return False

def make_dark_layer(doc, pg):
    """Add a layer that inverts the page content. First choice: a vector 'difference' blend layer (sharp at any zoom,
    tiny). If that doesn't render correctly, falls back to an inverted 200-dpi picture. Returns (xref, page)."""
    try:
        before = pg.get_pixmap(dpi=24, annots=False)
    except Exception:
        before = None
    a = None
    ok = False
    try:
        a = pg.add_stamp_annot(pg.rect, stamp=0)
        x = a.xref
        apx = int(re.search(r"(\d+) 0 R", doc.xref_get_key(x, "AP/N")[1]).group(1))
        rv = doc.xref_get_key(x, "Rect")[1].strip("[]").split()
        w, h = float(rv[2]) - float(rv[0]), float(rv[3]) - float(rv[1])
        gs = doc.get_new_xref()
        doc.update_object(gs, "<< /Type /ExtGState /BM /Difference /CA 1 /ca 1 >>")
        doc.xref_set_key(apx, "BBox", "[0 0 %g %g]" % (w, h))
        doc.xref_set_key(apx, "Matrix", "[1 0 0 1 0 0]")
        doc.xref_set_key(apx, "Resources", "<< /ExtGState << /GS0 %d 0 R >> >>" % gs)
        doc.update_stream(apx, ("/GS0 gs 1 1 1 rg 0 0 %g %g re f" % (w, h)).encode())
        a.set_info(subject=DARK)
        try:
            a.set_flags(fitz.PDF_ANNOT_IS_PRINT)
        except Exception:
            pass
        pg = reload(doc, pg)
        ok = True if before is None else _inverted_ok(pg, before)
    except Exception:
        ok = False
    if ok:
        for an in pg.annots(types=[fitz.PDF_ANNOT_STAMP]) or []:
            if ((an.info or {}).get("subject") or "") == DARK:
                return an.xref, pg
    # --- fallback: inverted picture of the page content
    try:
        if a is not None:
            pg.delete_annot(a)
    except Exception:
        pass
    pg = reload(doc, pg)
    remove_dark(pg)
    pg = reload(doc, pg)
    pix = pg.get_pixmap(dpi=200, annots=False)
    pix.invert_irect()
    a = None
    for src in (pix, pix.tobytes("png")):
        try:
            a = pg.add_stamp_annot(pg.rect, stamp=src)
            if a:
                break
        except Exception:
            a = None
    if not a:
        return None, pg
    a.set_info(subject=DARK)
    return a.xref, pg

def move_first(doc, pg, xref):
    """Put the dark layer at the bottom of the annotation stack, so ink and pictures sit on top of it."""
    xs = [t[0] for t in pg.annot_xrefs()]
    if xref in xs:
        xs.remove(xref)
    xs.insert(0, xref)
    doc.xref_set_key(pg.xref, "Annots", "[" + " ".join("%d 0 R" % x for x in xs) + "]")

def recolor_page(doc, pg, dark):
    """Rewrite ink/text colours so the saved file looks right on a light or a dark page."""
    pi = pg.number
    for an in list(pg.annots(types=[fitz.PDF_ANNOT_INK]) or []):
        try:
            sub = (an.info or {}).get("subject") or ""
            m = re.match(r"pdfink-lc=(#[0-9a-fA-F]{6})", sub)
            if m:
                lc = m.group(1).lower()
            else:
                st = (an.colors or {}).get("stroke") or (0, 0, 0)
                lc = "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in st[:3])
            an.set_colors(stroke=rgb_t(disp_color(lc, dark)))
            an.set_info(subject=LC + lc)
            an.update()
        except Exception:
            pass
    r = pg.rect
    texts = []
    for an in list(pg.annots(types=[fitz.PDF_ANNOT_FREE_TEXT]) or []):
        info = an.info or {}
        sub = info.get("subject") or ""
        txt = info.get("content", "") or ""
        m = re.search(r"pdfink:size=([0-9.]+);color=(#[0-9a-fA-F]{6})", sub)
        if not m or not txt.strip():
            continue
        fm = re.search(r";font=(helv|tiro|cour)", sub)
        fb, fi, fu = (re.search(r";%s=([01])" % k, sub) for k in "biu")
        q = (fitz.Rect(an.rect) * pg.rotation_matrix).normalize()
        texts.append((an, {"page": pi, "text": txt, "color": m.group(2), "size": float(m.group(1)),
                           "x": q.x0 / r.width, "y": q.y0 / r.height, "wd": q.width / r.width,
                           "font": fm.group(1) if fm else "helv",
                           "b": int(fb.group(1)) if fb else 0, "i": int(fi.group(1)) if fi else 0,
                           "u": int(fu.group(1)) if fu else 0}))
    if texts:
        ids = {an.xref for an, _ in texts}
        for ln in list(pg.annots(types=[fitz.PDF_ANNOT_LINE]) or []):
            sub = (ln.info or {}).get("subject") or ""
            if sub.startswith("pdfink-ul:"):
                try:
                    if int(sub.split(":")[1]) in ids:
                        pg.delete_annot(ln)
                except Exception:
                    pass
        for an, s in texts:
            try:
                pg.delete_annot(an)
                add_text(doc, s, dark)
            except Exception:
                pass

def full_save(doc, p, garbage):
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(p))
    os.close(fd)
    doc.save(tmp, garbage=garbage, deflate=True)
    doc.close()
    os.replace(tmp, p)

@app.route("/api/dark", methods=["POST"])
def api_dark():
    p = safe(request.args.get("path", ""))
    if not p.lower().endswith(".pdf") or not os.path.isfile(p):
        abort(404)
    b = request.get_json(force=True) or {}
    want = bool(b.get("dark"))
    pages = sorted({int(x) for x in b.get("pages", [])})
    backup_once(p)
    doc = fitz.open(p)
    try:
        for i in pages:
            if not 0 <= i < doc.page_count:
                continue
            pg = doc[i]
            if is_dark_page(pg) == want:
                continue
            remove_dark(pg)
            recolor_page(doc, pg, want)
            if want:
                pg = reload(doc, pg)
                x, pg = make_dark_layer(doc, pg)
                if x:
                    move_first(doc, pg, x)
        full_save(doc, p, 1)
    except Exception as e:
        try:
            doc.close()
        except Exception:
            pass
        return jsonify(error=str(e)), 500
    return jsonify(ok=True, m=os.path.getmtime(p))

# ---------------------------------------------------------------- save
@app.route("/api/save", methods=["POST"])
def api_save():
    p = safe(request.args.get("path", ""))
    if not p.lower().endswith(".pdf") or not os.path.isfile(p):
        abort(404)
    body = request.get_json(force=True) or {}
    strokes, dels = body.get("strokes", []), set(int(x) for x in body.get("delete", []))
    if not strokes and not dels:
        return jsonify(ok=True, added=0, xrefs=[], failed=0, m=os.path.getmtime(p))
    backup_once(p)
    doc = fitz.open(p)
    heavy = False
    if dels:
        for pg in doc:
            for an in [a for a in (pg.annots() or []) if a.xref in dels or
                       ((a.info.get("subject") or "").startswith("pdfink-ul:") and
                        int((a.info["subject"].split(":") + ["-1"])[1] or -1) in dels)]:
                if an.type[0] == fitz.PDF_ANNOT_STAMP:
                    heavy = True
                pg.delete_annot(an)
    touched = set()
    for s in strokes:
        if s.get("kind") != "img":
            try:
                touched.add(int(s.get("page", 0)))
            except Exception:
                pass
    dk = set()                        # which of the touched pages are dark (only those pages are checked: cheap)
    for i in touched:
        if 0 <= i < doc.page_count and is_dark_page(doc[i]):
            dk.add(i)
    xrefs = []
    for s in strokes:
        try:
            k = s.get("kind")
            pgi = int(s.get("page", 0))
            if k == "text":
                x = add_text(doc, s, pgi in dk)
            elif k == "img":
                x = add_image(doc, s)
            else:
                x = add_ink(doc, s, pgi in dk)
        except Exception:
            x = None
        xrefs.append(x)
    added = sum(1 for x in xrefs if x is not None)
    done = False
    if not heavy:
        try:
            if doc.can_save_incrementally():
                doc.saveIncr()
                doc.close()
                done = True
        except Exception:
            done = False
    if not done:
        full_save(doc, p, 1 if heavy else 0)
    return jsonify(ok=True, added=added, failed=len(xrefs) - added, xrefs=xrefs, m=os.path.getmtime(p))

@app.route("/api/mtime")
def api_mtime():
    p = safe(request.args.get("path", ""))
    if not os.path.isfile(p):
        abort(404)
    return jsonify(m=os.path.getmtime(p))

# ---------------------------------------------------------------- page templates / notebooks
SIZES = {"a4": (595.28, 841.89), "a4l": (841.89, 595.28), "a5": (419.53, 595.28), "letter": (612.0, 792.0)}

def draw_template(page, tpl):
    if tpl not in ("lined", "grid", "dots"):
        return
    w, h = page.rect.width, page.rect.height
    col = (0.74, 0.78, 0.88)
    sh = page.new_shape()
    if tpl == "lined":
        y = 72.0
        while y < h - 36:
            sh.draw_line((36, y), (w - 36, y))
            y += 26
        sh.finish(color=col, width=0.6)
    elif tpl == "grid":
        x = 0.0
        while x <= w:
            sh.draw_line((x, 0), (x, h))
            x += 20
        y = 0.0
        while y <= h:
            sh.draw_line((0, y), (w, y))
            y += 20
        sh.finish(color=col, width=0.4)
    else:
        y = 20.0
        while y < h - 10:
            x = 20.0
            while x < w - 10:
                sh.draw_circle((x, y), 0.9)
                x += 20
            y += 20
        sh.finish(color=None, fill=col)
    sh.commit()

@app.route("/api/new", methods=["POST"])
def api_new():
    b = request.get_json(force=True) or {}
    d = safe(b.get("folder", ""))
    if not os.path.isdir(d):
        abort(404)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(b.get("name", "")).strip()).strip(". ")
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    name = name[:100] or "Untitled notebook"
    full = os.path.join(d, name + ".pdf")
    if os.path.exists(full):
        return jsonify(error="A file with that name already exists"), 409
    w, h = SIZES.get(b.get("size", "a4"), SIZES["a4"])
    try:
        n = max(1, min(300, int(b.get("pages", 1))))
    except (TypeError, ValueError):
        n = 1
    tpl = b.get("template", "blank")
    doc = fitz.open()
    for _ in range(n):
        draw_template(doc.new_page(width=w, height=h), tpl)
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=d)
    os.close(fd)
    doc.save(tmp, deflate=True)
    doc.close()
    os.replace(tmp, full)
    return jsonify(ok=True, path=os.path.relpath(full, ROOT))

# ---------------------------------------------------------------- page management
@app.route("/api/pages", methods=["POST"])
def api_pages():
    p = safe(request.args.get("path", ""))
    if not p.lower().endswith(".pdf") or not os.path.isfile(p):
        abort(404)
    b = request.get_json(force=True) or {}
    act = b.get("action")
    backup_once(p)
    doc = fitz.open(p)
    n = doc.page_count
    try:
        i = int(b.get("index", 0))
        if not 0 <= i < n:
            abort(400)
        if act == "add":
            mb = doc[i].mediabox
            draw_template(doc.new_page(pno=i + 1, width=mb.width, height=mb.height), b.get("template", "blank"))
        elif act == "delete":
            if n < 2:
                abort(400)
            doc.delete_page(i)
        elif act == "move":
            t = int(b.get("to", i))
            if not 0 <= t < n:
                abort(400)
            if t != i:
                if t > i:
                    doc.move_page(i, t + 1 if t + 1 < n else -1)
                else:
                    doc.move_page(i, t)
        else:
            abort(400)
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(p))
        os.close(fd)
        doc.save(tmp, garbage=1, deflate=True)
        pages = doc.page_count
    finally:
        doc.close()
    os.replace(tmp, p)
    return jsonify(ok=True, pages=pages, m=os.path.getmtime(p))

if __name__ == "__main__":
    print(f"Serving PDFs from {ROOT} on :{PORT}" + ("" if PASSWORD else "  (WARNING: no APP_PASSWORD set)"))
    app.run(host="0.0.0.0", port=PORT)