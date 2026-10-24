import os
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import qrcode
from flask import Flask, flash, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
# Production can point this at a persistent mounted directory (for example
# /var/data on Render). Local development continues to use the project folder.
DATA_DIR = Path(os.environ.get("LUX_QR_DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# When moving the existing project to a persistent production directory for
# the first time, seed the persistent database from the supplied project DB.
# Never overwrite an existing production DB.
DB_PATH = DATA_DIR / "lux_qr.db"
if DATA_DIR != BASE_DIR and not DB_PATH.exists():
    bundled_db = BASE_DIR / "lux_qr.db"
    if bundled_db.exists():
        import shutil
        shutil.copy2(bundled_db, DB_PATH)

UPLOAD_DIR = DATA_DIR / "uploads"
QR_DIR = BASE_DIR / "static" / "qr"
ALLOWED_EXTENSIONS = {"xlsx", "xls", "csv"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "lux-qr-system-local-secret")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

UPLOAD_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

if QR_DIR.exists() and not QR_DIR.is_dir():
    raise RuntimeError(
        f"QR path exists but is not a directory: {QR_DIR}. "
        "Remove or rename the conflicting file."
    )

QR_DIR.mkdir(parents=True, exist_ok=True)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def seed_locations_from_bundled_db():
    """Restore permanent QR mappings into a persistent DB without deleting history.

    This handles the common deployment case where the persistent disk already
    contains an older/empty SQLite database. Existing rows are never replaced.
    """
    if DB_PATH == BASE_DIR / "lux_qr.db":
        return
    bundled_db = BASE_DIR / "lux_qr.db"
    if not bundled_db.exists() or not DB_PATH.exists():
        return

    src = sqlite3.connect(bundled_db)
    src.row_factory = sqlite3.Row
    dst = db()
    try:
        src_rows = src.execute("SELECT * FROM locations WHERE active=1 ORDER BY id").fetchall()
        for r in src_rows:
            exists = dst.execute("SELECT 1 FROM locations WHERE qr_id=?", (r["qr_id"],)).fetchone()
            if exists:
                continue
            dst.execute("""
                INSERT INTO locations
                (qr_id, location_key, sno, department, checking_area, lux_req, lux_reading,
                 checked_date, next_due_date, verified, qr_filename, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, tuple(r[k] for k in (
                "qr_id", "location_key", "sno", "department", "checking_area",
                "lux_req", "lux_reading", "checked_date", "next_due_date",
                "verified", "qr_filename", "active", "created_at", "updated_at"
            )))
        dst.commit()
    finally:
        src.close()
        dst.close()


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        qr_id TEXT NOT NULL UNIQUE,
        location_key TEXT NOT NULL UNIQUE,
        sno TEXT,
        department TEXT NOT NULL,
        checking_area TEXT NOT NULL,
        lux_req REAL,
        lux_reading REAL,
        checked_date TEXT,
        next_due_date TEXT,
        verified TEXT,
        qr_filename TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        qr_id TEXT NOT NULL,
        upload_id TEXT NOT NULL,
        month_label TEXT NOT NULL,
        uploaded_at TEXT NOT NULL,
        sno TEXT,
        department TEXT,
        checking_area TEXT,
        lux_req REAL,
        lux_reading REAL,
        checked_date TEXT,
        next_due_date TEXT,
        verified TEXT,
        source_file TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_history_qr ON history(qr_id);
    CREATE INDEX IF NOT EXISTS idx_history_month ON history(month_label);
    """)
    conn.commit()
    conn.close()


def normalize_text(value):
    if pd.isna(value):
        return ""
    s = str(value).replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s


def normalize_column(col):
    return normalize_text(col)


def clean_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def date_value(value):
    if pd.isna(value) or value == "":
        return None
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        return str(value).strip()
    return dt.strftime("%Y-%m-%d")


def number_value(value):
    if pd.isna(value) or value == "":
        return None
    n = pd.to_numeric(value, errors="coerce")
    if pd.isna(n):
        return None
    return float(n)


def read_lux_excel(file_path):
    """Read the user's workbook even when the real header is below a title row."""
    if str(file_path).lower().endswith(".csv"):
        raw = pd.read_csv(file_path, header=None)
    else:
        raw = pd.read_excel(file_path, header=None)

    header_row = None
    for i in range(min(len(raw), 25)):
        values = [normalize_column(v) for v in raw.iloc[i].tolist() if pd.notna(v)]
        has_sno = any(v in {"S.NO", "S NO", "SNO", "SERIAL NO", "SERIAL NUMBER"} for v in values)
        has_department = any("DEPARTMENT" in v or v == "DEPT" for v in values)
        has_checking_area = any("CHECKING AREA" in v or v == "CHECKING" for v in values)
        if has_sno and has_department and has_checking_area:
            header_row = i
            break

    if header_row is None:
        raise ValueError(
            "Could not find the Excel header row. Required columns include "
            "S.NO, DEPARTMENT and CHECKING AREA."
        )

    if str(file_path).lower().endswith(".csv"):
        df = pd.read_csv(file_path, header=header_row)
    else:
        df = pd.read_excel(file_path, header=header_row)

    df.columns = [normalize_column(c) for c in df.columns]
    df = df.dropna(how="all").reset_index(drop=True)

    # Remove signature/footer rows. Keep only rows having a numeric S.No.
    sno_col = next(
        (c for c in df.columns if c in {"S.NO", "S NO", "SNO", "SERIAL NO", "SERIAL NUMBER"}),
        None
    )
    if sno_col:
        numeric_sno = pd.to_numeric(df[sno_col], errors="coerce")
        df = df[numeric_sno.notna()].copy()
        df[sno_col] = numeric_sno.loc[df.index]
        df[sno_col] = df[sno_col].astype("Int64")

    department_col = next(
        (c for c in df.columns if c == "DEPARTMENT" or c == "DEPT" or "DEPARTMENT" in c),
        None
    )
    checking_area_col = next(
        (c for c in df.columns if c == "CHECKING AREA" or "CHECKING AREA" in c),
        None
    )
    lux_req_col = next(
        (c for c in df.columns if "LUX REQ" in c or "LUX REQUIRED" in c or "REQUIRED LUX" in c),
        None
    )
    lux_reading_col = next(
        (c for c in df.columns if "LUX READING" in c or "ACTUAL LUX" in c or "MEASURED LUX" in c),
        None
    )
    qr_col = next(
        (c for c in df.columns if c in {"QR ID", "QRID", "PERMANENT QR ID", "PERMANENT QR"}),
        None
    )
    checked_col = next((c for c in df.columns if "CHECKED DATE" in c), None)
    due_col = next((c for c in df.columns if "NEXT DUE DATE" in c), None)
    verified_col = next((c for c in df.columns if c == "VERIFIED" or "VERIFIED" in c), None)

    if not department_col:
        raise ValueError(f"Department column not found. Detected columns: {list(df.columns)}")
    if not checking_area_col:
        raise ValueError(f"Checking Area column not found. Detected columns: {list(df.columns)}")

    rename_map = {
        department_col: "DEPARTMENT",
        checking_area_col: "CHECKING_AREA",
    }
    if sno_col:
        rename_map[sno_col] = "SNO"
    if lux_req_col:
        rename_map[lux_req_col] = "LUX_REQ"
    if lux_reading_col:
        rename_map[lux_reading_col] = "LUX_READING"
    if qr_col:
        rename_map[qr_col] = "QR_ID"
    if checked_col:
        rename_map[checked_col] = "CHECKED_DATE"
    if due_col:
        rename_map[due_col] = "NEXT_DUE_DATE"
    if verified_col:
        rename_map[verified_col] = "VERIFIED"

    df = df.rename(columns=rename_map)

    for col in ["SNO", "QR_ID", "DEPARTMENT", "CHECKING_AREA", "LUX_REQ",
                "LUX_READING", "CHECKED_DATE", "NEXT_DUE_DATE", "VERIFIED"]:
        if col not in df.columns:
            df[col] = None

    # Normalize text fields while preserving numbers/dates.
    df["DEPARTMENT"] = df["DEPARTMENT"].map(normalize_text)
    df["CHECKING_AREA"] = df["CHECKING_AREA"].map(normalize_text)
    df["QR_ID"] = df["QR_ID"].map(normalize_text)

    return df


def make_location_key(department, checking_area):
    return f"{normalize_text(department)}|{normalize_text(checking_area)}"


def next_qr_id(conn):
    rows = conn.execute("SELECT qr_id FROM locations WHERE qr_id LIKE 'LUX-QR-%'").fetchall()
    max_num = 0
    for row in rows:
        m = re.fullmatch(r"LUX-QR-(\d+)", row["qr_id"] or "")
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"LUX-QR-{max_num + 1:05d}"


def public_base_url():
    """Return the public origin used inside permanent QR codes.

    Priority: explicit PUBLIC_BASE_URL, then Render's RENDER_EXTERNAL_URL.
    If neither exists, local request.host_url is used only for local testing.
    """
    configured = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if render_url:
        return render_url
    return ""


def validate_public_base_url(base_url):
    return base_url.rstrip("/") if base_url else ""


def request_public_base_url():
    try:
        return request.host_url.rstrip("/")
    except RuntimeError:
        return ""


def resolved_base_url():
    return public_base_url() or request_public_base_url()


def create_qr(qr_id, base_url=None, data=None):
    """Create a PERMANENT QR code. The QR contains only the permanent scan URL.

    Monthly Excel values are stored in SQLite history and the scan page always
    reads the latest values for this QR ID. The QR image therefore never needs
    to change when a new month is uploaded.
    """
    filename = f"{qr_id}.png"
    path = QR_DIR / filename
    origin = validate_public_base_url(base_url) if base_url else resolved_base_url()
    if not origin:
        origin = "http://127.0.0.1:5000"
    scan_url = f"{origin.rstrip('/')}/scan/{qr_id}"
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(scan_url)
    qr.make(fit=True)
    qr.make_image().save(path)
    return filename


def location_qr_data(row):
    return {
        "QR_ID": row["qr_id"],
        "S_NO": row["sno"],
        "DEPARTMENT": row["department"],
        "CHECKING_AREA": row["checking_area"],
        "LUX_REQUIRED": row["lux_req"],
        "LUX_READING": row["lux_reading"],
        "CHECKED_DATE": row["checked_date"],
        "NEXT_DUE_DATE": row["next_due_date"],
        "VERIFIED": row["verified"],
    }

def regenerate_all_qr_codes(base_url=None):
    """Rebuild existing QR PNGs with the same permanent QR IDs.

    The database mapping is untouched. This is intentionally done when a
    production PUBLIC_BASE_URL is configured so an old LAN URL can never
    remain embedded in a printed/generated QR image after deployment.
    """
    base_url = validate_public_base_url(base_url)
    conn = db()
    try:
        rows = conn.execute(
            "SELECT qr_id, qr_filename FROM locations WHERE active=1 ORDER BY id"
        ).fetchall()
        for row in rows:
            create_qr(row["qr_id"], base_url, location_qr_data(row))
    finally:
        conn.close()
    return len(rows)


def get_or_create_location(conn, row, base_url):
    department = normalize_text(row.get("DEPARTMENT"))
    area = normalize_text(row.get("CHECKING_AREA"))
    sno = clean_value(row.get("SNO"))
    supplied_qr = normalize_text(row.get("QR_ID"))
    key = make_location_key(department, area)

    existing = None
    if supplied_qr:
        existing = conn.execute(
            "SELECT * FROM locations WHERE qr_id = ?", (supplied_qr,)
        ).fetchone()

    if existing is None:
        existing = conn.execute(
            "SELECT * FROM locations WHERE location_key = ?", (key,)
        ).fetchone()

    # Last-resort matching by S.No. This keeps compatibility with old Excel files.
    if existing is None and sno is not None and str(sno).strip():
        existing = conn.execute(
            "SELECT * FROM locations WHERE sno = ? AND active = 1", (str(sno),)
        ).fetchone()

    now = datetime.now().isoformat(timespec="seconds")
    if existing:
        qr_id = existing["qr_id"]
        # If an old QR was created with another host, the PNG is still regenerated
        # on upload only if the file is missing. The printed QR remains the same ID.
        filename = existing["qr_filename"]
        create_qr(qr_id, base_url, {
            "QR_ID": qr_id, "S_NO": existing["sno"],
            "DEPARTMENT": existing["department"], "CHECKING_AREA": existing["checking_area"],
            "LUX_REQUIRED": existing["lux_req"], "LUX_READING": existing["lux_reading"],
            "CHECKED_DATE": existing["checked_date"], "NEXT_DUE_DATE": existing["next_due_date"],
            "VERIFIED": existing["verified"]})
        return qr_id, existing["id"], False

    qr_id = next_qr_id(conn)
    filename = f"{qr_id}.png"
    conn.execute("""
        INSERT INTO locations
        (qr_id, location_key, sno, department, checking_area,
         lux_req, lux_reading, checked_date, next_due_date, verified,
         qr_filename, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
    """, (
        qr_id, key, str(sno) if sno is not None else None,
        department, area, number_value(row.get("LUX_REQ")),
        number_value(row.get("LUX_READING")), date_value(row.get("CHECKED_DATE")),
        date_value(row.get("NEXT_DUE_DATE")), clean_value(row.get("VERIFIED")),
        filename, now, now
    ))
    return qr_id, conn.execute("SELECT last_insert_rowid()").fetchone()[0], True


def update_location(conn, qr_id, row):
    department = normalize_text(row.get("DEPARTMENT"))
    area = normalize_text(row.get("CHECKING_AREA"))
    sno = clean_value(row.get("SNO"))
    key = make_location_key(department, area)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("""
        UPDATE locations SET
            location_key=?, sno=?, department=?, checking_area=?,
            lux_req=?, lux_reading=?, checked_date=?, next_due_date=?,
            verified=?, active=1, updated_at=?
        WHERE qr_id=?
    """, (
        key, str(sno) if sno is not None else None, department, area,
        number_value(row.get("LUX_REQ")), number_value(row.get("LUX_READING")),
        date_value(row.get("CHECKED_DATE")), date_value(row.get("NEXT_DUE_DATE")),
        clean_value(row.get("VERIFIED")), now, qr_id
    ))


def month_label_from_df(df):
    if "CHECKED_DATE" in df.columns:
        dates = pd.to_datetime(df["CHECKED_DATE"], errors="coerce").dropna()
        if not dates.empty:
            return dates.max().strftime("%B %Y")
    return datetime.now().strftime("%B %Y")


@app.route("/")
def index():
    return redirect(url_for("admin"))


@app.route("/admin")
def admin():
    conn = db()
    locations = conn.execute(
        "SELECT * FROM locations WHERE active=1 ORDER BY id"
    ).fetchall()
    months = conn.execute(
        "SELECT month_label, MAX(uploaded_at) AS uploaded_at, COUNT(*) AS rows_count "
        "FROM history GROUP BY month_label ORDER BY uploaded_at DESC"
    ).fetchall()
    conn.close()
    return render_template("admin.html", locations=locations, months=months)


@app.post("/upload")
def upload_excel():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please choose an Excel file.", "danger")
        return redirect(url_for("admin"))

    ext = Path(file.filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_EXTENSIONS:
        flash("Only .xlsx, .xls or .csv files are allowed.", "danger")
        return redirect(url_for("admin"))

    upload_id = uuid.uuid4().hex
    safe_name = secure_filename(file.filename)
    saved_path = UPLOAD_DIR / f"{upload_id}_{safe_name}"
    file.save(saved_path)

    try:
        df = read_lux_excel(saved_path)
        base_url = resolved_base_url() or request.host_url.rstrip("/")
        month_label = month_label_from_df(df)
        uploaded_at = datetime.now().isoformat(timespec="seconds")

        conn = db()
        created = 0
        updated = 0

        for _, row in df.iterrows():
            if not normalize_text(row.get("DEPARTMENT")) or not normalize_text(row.get("CHECKING_AREA")):
                continue

            qr_id, _, was_created = get_or_create_location(conn, row, base_url)
            update_location(conn, qr_id, row)
            conn.execute("""
                INSERT INTO history
                (qr_id, upload_id, month_label, uploaded_at, sno, department,
                 checking_area, lux_req, lux_reading, checked_date,
                 next_due_date, verified, source_file)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                qr_id, upload_id, month_label, uploaded_at,
                str(clean_value(row.get("SNO"))) if clean_value(row.get("SNO")) is not None else None,
                normalize_text(row.get("DEPARTMENT")),
                normalize_text(row.get("CHECKING_AREA")),
                number_value(row.get("LUX_REQ")),
                number_value(row.get("LUX_READING")),
                date_value(row.get("CHECKED_DATE")),
                date_value(row.get("NEXT_DUE_DATE")),
                clean_value(row.get("VERIFIED")),
                safe_name
            ))

            if was_created:
                created += 1
            else:
                updated += 1

        conn.commit()
        conn.close()
        flash(
            f"Upload successful: {len(df)} rows processed. "
            f"{created} permanent QR codes created, {updated} existing locations updated. "
            f"Previous monthly data was preserved in history.",
            "success"
        )
    except Exception as exc:
        flash(f"Upload failed: {exc}", "danger")
    finally:
        try:
            saved_path.unlink(missing_ok=True)
        except Exception:
            pass

    return redirect(url_for("admin"))


@app.route("/qr_codes")
def qr_codes():
    # On the deployed public service, refresh QR PNGs with the public HTTPS
    # origin while preserving every permanent QR ID.
    origin = resolved_base_url()
    if origin:
        try:
            regenerate_all_qr_codes(origin)
        except Exception:
            pass
    conn = db()
    locations = conn.execute("SELECT * FROM locations WHERE active=1 ORDER BY id").fetchall()
    conn.close()
    return render_template("qr_codes.html", locations=locations, public_base_url=origin)


@app.post("/regenerate_qr")
def regenerate_qr_route():
    try:
        base_url = resolved_base_url() or request.host_url.rstrip("/")
        count = regenerate_all_qr_codes(base_url)
        flash(f"Regenerated {count} permanent QR codes using {base_url}.", "success")
    except Exception as exc:
        flash(f"QR regeneration failed: {exc}", "danger")
    return redirect(url_for("qr_codes"))


@app.route("/qr/<path:filename>")
def qr_file(filename):
    response = send_from_directory(QR_DIR, filename)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/scan/<qr_id>")
def scan(qr_id):
    """Public scan page. The QR ID is permanent; only the data behind it changes."""
    qr_id = normalize_text(qr_id)
    conn = db()
    try:
        location = conn.execute(
            "SELECT * FROM locations WHERE UPPER(qr_id)=? AND active=1", (qr_id,)
        ).fetchone()
        history = conn.execute(
            "SELECT * FROM history WHERE UPPER(qr_id)=? "
            "ORDER BY uploaded_at DESC, id DESC",
            (qr_id,)
        ).fetchall()
    finally:
        conn.close()

    if location is None:
        return render_template(
            "error.html",
            message=f"QR code {qr_id} was not found. Please check that the permanent QR database is present.",
        ), 404

    return render_template("scan.html", location=location, history=history)


@app.get("/api/scan/<qr_id>")
def scan_api(qr_id):
    """Machine-readable version of the same permanent QR data."""
    qr_id = normalize_text(qr_id)
    conn = db()
    try:
        location = conn.execute(
            "SELECT * FROM locations WHERE UPPER(qr_id)=? AND active=1", (qr_id,)
        ).fetchone()
        history = conn.execute(
            "SELECT * FROM history WHERE UPPER(qr_id)=? ORDER BY uploaded_at DESC, id DESC",
            (qr_id,),
        ).fetchall()
    finally:
        conn.close()

    if location is None:
        return {"ok": False, "error": "QR code not found", "qr_id": qr_id}, 404

    def row_dict(r):
        return {k: r[k] for k in r.keys()}

    return {
        "ok": True,
        "qr_id": location["qr_id"],
        "current": row_dict(location),
        "history": [row_dict(h) for h in history],
    }


@app.route("/history")
def history():
    month = request.args.get("month", "").strip()
    conn = db()
    months = conn.execute(
        "SELECT DISTINCT month_label FROM history ORDER BY uploaded_at DESC"
    ).fetchall()
    if month:
        rows = conn.execute(
            "SELECT * FROM history WHERE month_label=? ORDER BY CAST(sno AS INTEGER), id",
            (month,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM history ORDER BY uploaded_at DESC, id DESC"
        ).fetchall()
    conn.close()
    return render_template("history.html", rows=rows, months=months, selected_month=month)


@app.get("/health")
def health():
    return {"status": "ok"}


# Gunicorn imports this module without executing the __main__ block, so the
# database must be initialized at import time for public deployment.
init_db()
seed_locations_from_bundled_db()

# If a public HTTPS origin is configured, refresh all existing QR PNGs while
# preserving every permanent QR ID and all monthly history.
_CONFIGURED_PUBLIC_BASE_URL = public_base_url()
if _CONFIGURED_PUBLIC_BASE_URL:
    regenerate_all_qr_codes(_CONFIGURED_PUBLIC_BASE_URL)


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print("=" * 60)
    print("LUX QR SYSTEM - Permanent QR / Monthly Excel History")
    print(f"Local: http://127.0.0.1:{port}/admin")
    print("For worldwide QR access, set PUBLIC_BASE_URL to your public HTTPS URL.")
    if _CONFIGURED_PUBLIC_BASE_URL:
        print(f"Public QR origin: {_CONFIGURED_PUBLIC_BASE_URL}")
    print("=" * 60)
    app.run(host=host, port=port, debug=debug)
