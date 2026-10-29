"""PDF ink editor: browse PDFs in a server folder, draw on them with a stylus, save in place.
Run:  pip install -r requirements.txt
      APP_PASSWORD=secret ROOT=~/remote_storage python app.py
"""
import os, re, shutil, tempfile, base64
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, send_file, send_from_directory, abort, Response

ROOT = os.path.realpath(os.path.expanduser(os.environ.get("ROOT", "~/remote_storage")))
USER = os.environ.get("APP_USER", "admin")
PASSWORD = os.environ.get("APP_PASSWORD", "")
PORT = int(os.environ.get("PORT", "8090"))
app = Flask(__name__, static_folder="static", static_url_path="/static")

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
    bak = p + ".orig.bak"             # keep the safety backup next to the renamed file
    if os.path.exists(bak) and not os.path.exists(new + ".orig.bak"):
        os.rename(bak, new + ".orig.bak")
    return jsonify(ok=True, path=os.path.relpath(new, ROOT))

# ---------------------------------------------------------------- read ink / text / pictures
@app.route("/api/strokes")
def api_strokes():
    """Existing ink + text + pictures in the PDF, as editable items (normalised 0-1 coords)."""
    p = safe(request.args.get("path", ""))
    if not p.lower().endswith(".pdf") or not os.path.isfile(p):
        abort(404)
    out = []
    doc = fitz.open(p)
    for pi, pg in enumerate(doc):
        r = pg.rect
        for an in pg.annots(types=[fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_FREE_TEXT, fitz.PDF_ANNOT_STAMP]) or []:
            t = an.type[0]
            if t == fitz.PDF_ANNOT_STAMP:                        # picture placed by this app
                if not ((an.info or {}).get("subject") or "").startswith("pdfink-img"):
                    continue
                q = (fitz.Rect(an.rect) * pg.rotation_matrix).normalize()
                out.append({"kind": "img", "page": pi, "xref": an.xref,
                            "x": q.x0 / r.width, "y": q.y0 / r.height,
                            "wd": q.width / r.width, "ht": q.height / r.height})
                continue
            if t == fitz.PDF_ANNOT_FREE_TEXT:                    # text box
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
    return jsonify(m=os.path.getmtime(p), strokes=out)

@app.route("/api/img")
def api_img():
    """PNG of a picture annotation (only used when a PDF is opened fresh; the browser keeps the original after that)."""
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

# ---------------------------------------------------------------- write
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
    bak = p + ".orig.bak"            # one-time safety copy of the untouched original
    if not os.path.exists(bak):
        shutil.copy2(p, bak)

def add_text(doc, s):
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
    c = s["color"].lstrip("#")
    rgb = tuple(int(c[i:i+2], 16) / 255 for i in (0, 2, 4))
    a = pg.add_freetext_annot(rect.normalize(), text, fontsize=fs, fontname=fn,
                              text_color=rgb, fill_color=None, align=0)
    if not a:
        return None
    a.set_info(subject="pdfink:size=%.5f;color=%s;font=%s;b=%d;i=%d;u=%d" % (
        size, s["color"].lower(), fkey,
        1 if s.get("b") else 0, 1 if s.get("i") else 0, 1 if s.get("u") else 0))
    xref = a.xref
    if s.get("u"):                    # FreeText has no underline: draw a companion line per wrapped row
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
    """Add a picture as a Stamp annotation (movable later). Returns its xref,
    -1 if it had to be merged into the page itself (visible, but not movable), or None on failure."""
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
    pg.insert_image(rect, stream=raw, overlay=True)     # fallback: becomes part of the page
    return -1

def add_ink(doc, s):
    """Add one pen/highlighter stroke as an Ink annotation. Returns its xref (or None)."""
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", s.get("color", "")) or len(s.get("pts", [])) < 2:
        return None
    pg = doc[int(s["page"])]
    r = pg.rect
    pts = []
    for x, y in s["pts"]:
        q = fitz.Point(x * r.width, y * r.height) * pg.derotation_matrix
        pts.append((float(q.x), float(q.y)))
    a = pg.add_ink_annot([pts])
    c = s["color"].lstrip("#")
    a.set_colors(stroke=tuple(int(c[i:i+2], 16) / 255 for i in (0, 2, 4)))
    a.set_border(width=max(0.3, float(s["w"]) * r.width))
    a.set_opacity(float(s.get("a", 1)))
    a.update()
    return a.xref

def full_save(doc, p, garbage):
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(p))
    os.close(fd)
    doc.save(tmp, garbage=garbage, deflate=True)
    doc.close()
    os.replace(tmp, p)               # atomic overwrite, same folder, same name

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
    heavy = False                     # deleting a picture needs a full rewrite so its data is dropped
    if dels:                          # erase previously saved items (and underline companions)
        for pg in doc:
            for an in [a for a in (pg.annots() or []) if a.xref in dels or
                       ((a.info.get("subject") or "").startswith("pdfink-ul:") and
                        int((a.info["subject"].split(":") + ["-1"])[1] or -1) in dels)]:
                if an.type[0] == fitz.PDF_ANNOT_STAMP:
                    heavy = True
                pg.delete_annot(an)
    xrefs = []                        # one entry per incoming item, same order (None = not added)
    for s in strokes:
        try:
            k = s.get("kind")
            x = add_text(doc, s) if k == "text" else add_image(doc, s) if k == "img" else add_ink(doc, s)
        except Exception:
            x = None
        xrefs.append(x)
    added = sum(1 for x in xrefs if x is not None)
    done = False
    if not heavy:                     # normal case: just append the changes (much cheaper than rewriting the file)
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
    """Draw a light background pattern: lined / grid / dots (blank = nothing)."""
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
    """Create a new notebook (PDF) inside a folder under ROOT."""
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
    """action: add (blank page after `index`, with `template`), delete, move (`index` -> final position `to`)."""
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