import sqlite3
import paho.mqtt.client as mqtt
import json
import threading
import time
import requests
import csv
import io
import os
from datetime import datetime
from flask import Flask, render_template_string, jsonify, request, Response

# ================== KONFIGURASI ==================
BROKER = "103.217.145.168"
PORT = 1883
TOPIC = "data/sctkiotserver/groupsctkiotserver/123"

WEB_APP_URL = "https://script.google.com/macros/s/AKfycbzWJVmsuj6p0-JKzksnPcdRkfH0NKa9n0iI_HP2OBaVHbxNQqYaSDGkzbdSraE0sFg-/exec"
SEND_INTERVAL = 60  # detik

# ====== QC CSV (Google Sheets publish CSV) ======
QC_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSMKrU7GU9pisN4ihKgSqyC1bDuT1ia6kp-vKWrdUhvaPyX95ZqOBOFy8iBCpQieizqTBJ3R4wNmRII/pub?gid=2046456175&single=true&output=csv"
QC_PULL_INTERVAL = 20  # detik

# ====== JADWAL (JSON lokal) ======
# >>>>>>>>>>>> UDAH DIUBAH: pakai 1 file setahun <<<<<<<<<<<<
# Taruh file JSON ini di folder yang sama dengan file python ini
SCHEDULE_JSON_FILE = "jadwal_2026.json"
SCHEDULE_RELOAD_INTERVAL = 10  # detik cek perubahan file

# ====== MODEL RESERVOIR ======
RES_MAX_M = 8.0
RES_TOTAL_M3 = 3000.0
RES_LITER_PER_M = (RES_TOTAL_M3 * 1000.0) / RES_MAX_M  # 375000 L per 1 meter

# ================== PARAMETER MQTT ==================
NUMERIC_KEYS = [
    "PRESSURE_DST",
    "LVL_RES_WTP3",
    "TOTAL_FLOW_ITK",
    "TOTAL_FLOW_DST",
    "FLOW_WTP3",
    "FLOW_50_WTP1",
    "FLOW_CIJERUK",
    "FLOW_CARENANG",
]
DERIVED_KEYS = ["SELISIH_FLOW"]

DISPLAY_ORDER = [
    "TOTAL_FLOW_ITK",
    "TOTAL_FLOW_DST",
    "SELISIH_FLOW",
    "FLOW_WTP3",
    "FLOW_50_WTP1",
    "FLOW_CIJERUK",
    "FLOW_CARENANG",
]

TITLE_MAP = {
    "PRESSURE_DST": "PRESSURE DISTRIBUSI",
    "LVL_RES_WTP3": "LEVEL RESERVOIR WTP 3",
    "TOTAL_FLOW_ITK": "TOTAL FLOW INTAKE",
    "TOTAL_FLOW_DST": "TOTAL FLOW DISTRIBUSI",
    "SELISIH_FLOW": "SELISIH TOTAL FLOW (INTAKE - DISTRIBUSI)",
    "FLOW_WTP3": "FLOW WTP 3",
    "FLOW_50_WTP1": "FLOW UPAM CIKANDE",
    "FLOW_CIJERUK": "FLOW UPAM CIJERUK",
    "FLOW_CARENANG": "FLOW UPAM CARENANG",
}

UNIT_MAP = {
    "PRESSURE_DST": "BAR",
    "LVL_RES_WTP3": "M",
    "TOTAL_FLOW_ITK": "LPS",
    "TOTAL_FLOW_DST": "LPS",
    "SELISIH_FLOW": "LPS",
    "FLOW_WTP3": "LPS",
    "FLOW_50_WTP1": "LPS",
    "FLOW_CIJERUK": "LPS",
    "FLOW_CARENANG": "LPS",
}

# ================== QC PARAM ==================
QC_PARAMS = {
    "kekeruhan": {"label": "KEKERUHAN", "col": "Kekeruhan", "unit": "NTU"},
    "warna": {"label": "WARNA", "col": "Warna", "unit": "TCU"},
    "ph": {"label": "PH", "col": "pH", "unit": ""},
    "sisa_chlor": {"label": "SISA CHLOR", "col": "Sisa Chlor", "unit": "MG/L"},
}
QC_ORDER = ["kekeruhan", "warna", "ph", "sisa_chlor"]

# ================== GLOBAL ==================
DB_PATH = "history.db"
db_lock = threading.Lock()
data_lock = threading.Lock()

DEFAULT_DATA = {k: 0.0 for k in (NUMERIC_KEYS + DERIVED_KEYS)}
latest_data = DEFAULT_DATA.copy()
latest_ts_epoch = 0
last_send_time = 0.0

# QC cache
qc_lock = threading.Lock()
qc_rows = []
qc_latest = {p: {"ts": None, "dt": "-", "value": None} for p in QC_ORDER}
qc_last_update_dt = "-"
qc_last_update_chlor_dt = "-"

qc_status = {
    "last_success_dt": "-",
    "last_error": None,
    "row_count": 0,
    "headers": [],
}

# Jadwal cache
schedule_lock = threading.Lock()
schedule_rows = []         # list of dict
schedule_last_loaded = "-" # dt string
schedule_last_error = None
_schedule_mtime = None

# ================== DB ==================
def init_db():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS measurements (
                ts INTEGER NOT NULL,
                key TEXT NOT NULL,
                value REAL NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_measurements_key_ts ON measurements(key, ts)")
        conn.commit()

def save_to_db(ts_epoch: int, data: dict):
    with db_lock:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            cur = conn.cursor()
            rows = [(ts_epoch, k, float(v)) for k, v in data.items()]
            cur.executemany("INSERT INTO measurements(ts, key, value) VALUES (?, ?, ?)", rows)
            conn.commit()

# ================== QC helpers ==================
def _to_float(v):
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() in ("nan", "none"):
        return None
    s = s.replace('"', "").strip()
    s = s.replace(",", ".")
    try:
        return float(s)
    except:
        return None

def _parse_dt(s):
    if not s:
        return None
    s = str(s).strip().replace('"', "")
    fmts = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]
    for f in fmts:
        try:
            return datetime.strptime(s, f)
        except:
            pass
    return None

def _norm_header(s: str) -> str:
    if s is None:
        return ""
    return str(s).replace("\ufeff", "").replace("\n", " ").replace("\r", " ").strip().lower().replace(" ", "")

def _find_col(fieldnames, candidates):
    if not fieldnames:
        return None
    norm_map = {_norm_header(h): h for h in fieldnames}
    for c in candidates:
        k = _norm_header(c)
        if k in norm_map:
            return norm_map[k]
    for c in candidates:
        k = _norm_header(c)
        for nk, orig in norm_map.items():
            if k and k in nk:
                return orig
    return None

def pull_qc_csv_once():
    global qc_rows, qc_latest, qc_last_update_dt, qc_last_update_chlor_dt, qc_status
    try:
        sep = "&" if "?" in QC_CSV_URL else "?"
        url = QC_CSV_URL + f"{sep}_={int(time.time())}"

        r = requests.get(
            url,
            timeout=25,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (QC-Dashboard)"},
        )
        r.raise_for_status()

        f = io.StringIO(r.text)
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        qc_status["headers"] = fieldnames

        dt_col = _find_col(fieldnames, ["DateTime", "Datetime", "DATE TIME", "Date Time"])
        kek_col = _find_col(fieldnames, ["Kekeruhan"])
        war_col = _find_col(fieldnames, ["Warna"])
        ph_col  = _find_col(fieldnames, ["pH", "PH"])
        chl_col = _find_col(fieldnames, ["Sisa Chlor", "SisaChlor"])

        rows = []
        for row in reader:
            dt_obj = _parse_dt(row.get(dt_col) if dt_col else None)
            if not dt_obj:
                continue
            ts = int(dt_obj.timestamp())
            rows.append({
                "ts": ts,
                "dt": dt_obj.strftime("%Y-%m-%d %H:%M"),
                "kekeruhan": _to_float(row.get(kek_col)) if kek_col else None,
                "warna": _to_float(row.get(war_col)) if war_col else None,
                "ph": _to_float(row.get(ph_col)) if ph_col else None,
                "sisa_chlor": _to_float(row.get(chl_col)) if chl_col else None,
            })

        rows.sort(key=lambda x: x["ts"])

        latest_map = {p: {"ts": None, "dt": "-", "value": None} for p in QC_ORDER}

        for p in ["kekeruhan", "warna", "ph"]:
            for rr in reversed(rows):
                if rr.get(p) is not None:
                    latest_map[p] = {"ts": rr["ts"], "dt": rr["dt"], "value": rr[p]}
                    break

        last_chlor_dt = "-"
        for rr in reversed(rows):
            if rr.get("sisa_chlor") is not None:
                latest_map["sisa_chlor"] = {"ts": rr["ts"], "dt": rr["dt"], "value": rr["sisa_chlor"]}
                last_chlor_dt = rr["dt"]
                break

        cand = [latest_map["kekeruhan"]["ts"], latest_map["warna"]["ts"], latest_map["ph"]["ts"]]
        cand = [x for x in cand if x is not None]
        last_qc_dt = "-"
        if cand:
            last_qc_ts = max(cand)
            for rr in reversed(rows):
                if rr["ts"] == last_qc_ts:
                    last_qc_dt = rr["dt"]
                    break

        with qc_lock:
            qc_rows[:] = rows
            qc_latest.clear()
            qc_latest.update(latest_map)
            qc_last_update_dt = last_qc_dt
            qc_last_update_chlor_dt = last_chlor_dt

        qc_status["last_success_dt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        qc_status["last_error"] = None
        qc_status["row_count"] = len(rows)

    except Exception as e:
        qc_status["last_error"] = str(e)
        print("[QC] pull error:", e)

def qc_worker():
    pull_qc_csv_once()
    while True:
        time.sleep(QC_PULL_INTERVAL)
        pull_qc_csv_once()

def qc_history(param: str, hours: float, interval: int):
    if param not in QC_PARAMS:
        return []
    now = int(time.time())
    start = now - int(hours * 3600)

    with qc_lock:
        rows = list(qc_rows)

    filtered = [r for r in rows if r["ts"] >= start and r.get(param) is not None]
    if not filtered:
        return []

    buckets = {}
    for r in filtered:
        b = (r["ts"] // interval) * interval
        buckets.setdefault(b, []).append(r[param])

    out = []
    for b in sorted(buckets.keys()):
        vals = buckets[b]
        out.append({"ts": int(b), "value": float(sum(vals) / max(len(vals), 1))})
    return out

# ================== JADWAL helpers ==================
def _ms_to_datestr(ms):
    try:
        # JSON tanggal biasanya ms epoch di midnight UTC -> aman pakai utcfromtimestamp
        dt = datetime.utcfromtimestamp(int(ms) / 1000.0)
        return dt.strftime("%Y-%m-%d")
    except:
        return None

def _load_schedule_file_if_changed(force=False):
    global schedule_rows, schedule_last_loaded, schedule_last_error, _schedule_mtime

    try:
        if not os.path.exists(SCHEDULE_JSON_FILE):
            raise FileNotFoundError(f"File jadwal tidak ditemukan: {SCHEDULE_JSON_FILE}")

        mtime = os.path.getmtime(SCHEDULE_JSON_FILE)
        if (not force) and (_schedule_mtime is not None) and (mtime == _schedule_mtime):
            return  # tidak berubah

        with open(SCHEDULE_JSON_FILE, "r", encoding="utf-8") as f:
            rows = json.load(f)

        if not isinstance(rows, list):
            raise ValueError("Format jadwal JSON harus list of objects")

        cleaned = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            cleaned.append({
                "tanggal": r.get("tanggal"),
                "nama": r.get("nama"),
                "jabatan": r.get("jabatan"),
                "shift_kode": r.get("shift_kode"),
                "jam_kerja": r.get("jam_kerja"),
                "lokasi": r.get("lokasi"),
                "jam_mulai": r.get("jam_mulai"),
                "jam_selesai": r.get("jam_selesai"),
            })

        with schedule_lock:
            schedule_rows = cleaned
            schedule_last_loaded = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            schedule_last_error = None
            _schedule_mtime = mtime

    except Exception as e:
        with schedule_lock:
            schedule_last_error = str(e)
            schedule_last_loaded = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("[SCHEDULE] load error:", e)

def schedule_worker():
    _load_schedule_file_if_changed(force=True)
    while True:
        time.sleep(SCHEDULE_RELOAD_INTERVAL)
        _load_schedule_file_if_changed(force=False)

def _schedule_for_date(date_str):
    with schedule_lock:
        rows = list(schedule_rows)

    op = []
    lab = []

    for r in rows:
        d = _ms_to_datestr(r.get("tanggal"))
        if d != date_str:
            continue

        nama = (r.get("nama") or "").strip()
        jab = (r.get("jabatan") or "").strip().lower()
        kode = (r.get("shift_kode") or "").strip().upper()
        jam  = (r.get("jam_kerja") or "").strip()
        lokasi = (r.get("lokasi") or "").strip().upper()

        # hanya yang benar-benar kerja (bukan OFF)
        if not r.get("jam_mulai") or not r.get("jam_selesai"):
            continue

        # ============ OPERATOR PRODUKSI: hanya WTP3 + hanya yang ada "12" ============
        if jab == "operator produksi":
            if lokasi != "WTP3":
                continue

            # ambil yang ada angka 12 saja (M12, P12, S12, N12, 12, A12, dll)
            if "12" not in kode:
                continue

            op.append({
                "nama": nama,
                "kode": kode or "-",
                "jam": jam or "-",
                "lokasi": "WTP3",
            })

        # ============ ANALIS LAB: hanya LAB ============
        elif jab == "analis laboratorium":
            if lokasi != "LAB":
                continue

            lab.append({
                "nama": nama,
                "kode": kode or "-",
                "jam": jam or "-",
                "lokasi": "LAB",
            })

    op.sort(key=lambda x: x["nama"])
    lab.sort(key=lambda x: x["nama"])
    return op, lab

# ================== FLASK ==================
app = Flask(__name__)

HTML_PAGE = r"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>WATER DISTRIBUTION PERFORMANCE & WATER QUALITY DASHBOARD</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
:root{
  --bgBase:#070a12;
  --panelA: rgba(16,24,38,0.92);
  --panelB: rgba(10,16,28,0.86);
  --stroke: rgba(255,255,255,0.12);
  --grid: rgba(255,255,255,0.18);
  --text:#eaf2ff;
  --muted: rgba(234,242,255,0.70);
  --accent:#2d86ff;
  --accentFill: rgba(45,134,255,0.18);
  --darkArc: rgba(255,170,90,0.22);
  --alarm:#ff4d6d;
  --pos:#22c55e;
  --neg:#ef4444;
  --bgRad1: radial-gradient(1200px 700px at 50% -10%, rgba(45,134,255,0.18), transparent 55%);
  --bgRad2: radial-gradient(900px 600px at 15% 20%, rgba(255,170,90,0.08), transparent 58%);
}
body[data-theme="light"]{
  --bgBase:#f2f6fb;
  --panelA: rgba(255,255,255,0.94);
  --panelB: rgba(246,249,255,0.90);
  --stroke: rgba(15, 23, 42, 0.16);
  --grid: rgba(15, 23, 42, 0.22);
  --text:#0b1220;
  --muted: rgba(11,18,32,0.70);
  --accent:#2563eb;
  --accentFill: rgba(37,99,235,0.16);
  --darkArc: rgba(217,119,6,0.16);
  --alarm:#e11d48;
  --pos:#16a34a;
  --neg:#dc2626;
  --bgRad1: radial-gradient(1200px 700px at 50% -10%, rgba(37,99,235,0.10), transparent 60%);
  --bgRad2: radial-gradient(900px 600px at 15% 20%, rgba(217,119,6,0.06), transparent 62%);
}

body{ font-family: Inter, Arial, sans-serif; background: var(--bgRad1), var(--bgRad2), var(--bgBase);
  color: var(--text); padding: 16px 18px 26px; margin:0; }
.topbar{
  display:flex; align-items:center; justify-content:center;
  margin: 2px 0 10px;
  text-transform: uppercase; font-weight: 900; letter-spacing: 0.9px; font-size: 28px;
  position: relative;
}
.topbarInner{ display:flex; align-items:center; justify-content:center; gap: 14px; }
.brandLogo{ height: 54px; width: auto; display:block; }
@media (max-width: 700px){
  .brandLogo{ height: 40px; }
  .topbar{ font-size: 20px; }
}
.themeBtn{
  position: absolute; right: 0; top: 50%;
  transform: translateY(-50%);
  background: rgba(255,255,255,0.06);
  border:1px solid var(--stroke);
  color: var(--text);
  padding: 8px 12px;
  border-radius: 10px;
  cursor: pointer;
  font-weight: 900;
  text-transform: uppercase;
}

/* pause/play button */
.pauseBtn{
  position: absolute;
  right: 78px;
  top: 50%;
  transform: translateY(-50%);
  width: 38px;
  height: 38px;
  border-radius: 10px;
  background: rgba(255,255,255,0.06);
  border:1px solid var(--stroke);
  color: var(--text);
  cursor: pointer;
  display:flex;
  align-items:center;
  justify-content:center;
  padding: 0;
  user-select:none;
}
.pauseBtn:hover{ background: rgba(255,255,255,0.10); }
.pauseBtn:active{ transform: translateY(-50%) scale(0.98); }
body[data-theme="light"] .pauseBtn{ background: rgba(7,20,39,0.04); }
.pauseBtn svg{ width: 16px; height: 16px; }
.pauseBtn.paused{ opacity: 0.95; }

hr{ border:0; border-top:1px solid var(--stroke); margin: 10px 0 14px; }

.card, .panel{
  background: linear-gradient(180deg, var(--panelA), var(--panelB));
  border: 1px solid var(--stroke);
  border-radius: 12px;
  padding: 12px;
  box-shadow: 0 10px 40px rgba(0,0,0,0.25);
  position: relative;
  overflow: hidden;
}

.sectionTitle{
  margin: 14px 0 8px;
  font-weight: 900;
  letter-spacing: .6px;
  text-transform: uppercase;
  color: var(--muted);
}

.grid{
  display:grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 14px;
  align-items: stretch;
}
@media (max-width: 1500px){ .grid{ grid-template-columns: repeat(4, 1fr); } }
@media (max-width: 1200px){ .grid{ grid-template-columns: repeat(2, 1fr); } }

.card{
  display:flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  text-align:center;
  min-height: 300px;
}

.titleRow{ width:100%; display:flex; justify-content:center; margin-bottom:6px; }
.title{
  font-size:12px; color: var(--muted);
  letter-spacing:0.6px; text-transform: uppercase; font-weight: 900;
}
.valueRow{ width:100%; display:flex; justify-content:center; gap:8px; margin-bottom:6px; }
.big{ font-size:28px; font-weight:900; }
.unit{ font-size:12px; color: var(--muted); font-weight:900; text-transform: uppercase; }

.valPos{ color: var(--pos) !important; }
.valNeg{ color: var(--neg) !important; }

.panel{ margin-top: 14px; padding: 12px; border-radius: 12px; }
.panelTitle{
  font-size: 13px; font-weight: 900;
  letter-spacing:0.6px; text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 10px 0;
  display:flex; justify-content:space-between; gap:12px; align-items:center;
}
.panelTitleLeft{
  display:flex; align-items:center; gap:10px;
}
.panelIcon{
  width: 28px; height: 28px;
  border-radius: 10px;
  border: 1px solid var(--stroke);
  background: rgba(255,255,255,0.06);
  display:flex; align-items:center; justify-content:center;
}
.panelIcon svg{ width: 16px; height: 16px; opacity: .9; }
body[data-theme="light"] .panelIcon{ background: rgba(7,20,39,0.04); }

.panelControls{ display:flex; gap: 10px; align-items:center; justify-content:flex-end; flex-wrap:wrap; }
select.dd, input.dd, button.ddBtn{
  appearance:none;
  background: rgba(255,255,255,0.06);
  border:1px solid var(--stroke);
  color: var(--text);
  padding: 8px 12px;
  border-radius: 10px;
  cursor: pointer;
  font-weight: 900;
  text-transform: uppercase;
  outline: none;
}
button.ddBtn{ user-select:none; }
button.ddBtn:active{ transform: scale(0.98); }
body[data-theme="light"] select.dd,
body[data-theme="light"] input.dd,
body[data-theme="light"] button.ddBtn{ background: rgba(7,20,39,0.04); }
select.dd option{ background: var(--panelA); color: var(--text); }
body[data-theme="light"] select.dd option{ background: rgba(255,255,255,1); color: var(--text); }

.bigChartWrap{ height: 280px; }
canvas.big{ width:100% !important; height:280px !important; }

.hint{ font-size: 12px; color: var(--muted); margin-top: 8px; text-transform: uppercase; }

.gaugeWrap{ width:100%; height:150px; display:flex; justify-content:center; }
#gauge_pressure{ width: 260px !important; height:150px !important; }
.rangeText{ font-size:11px; color: var(--muted); text-transform: uppercase; }

.reservoirArea{
  width:100%;
  max-width: 380px;
  display:grid;
  grid-template-columns: 50px 1fr;
  gap: 10px;
  align-items: stretch;
  margin-top: 6px;
}
.yLabels{ position:relative; border-right:1px solid var(--stroke); }
.yLabels .yl{
  position:absolute; left:0; width:50px;
  transform: translateY(50%);
  font-size:11px; font-weight:900; color: var(--muted); text-transform: uppercase;
}
.yLabels .yl::after{
  content:"";
  position:absolute; left:46px; top:50%;
  width:14px; height:1px;
  background: var(--grid);
  transform: translateY(-50%);
}
.tankBox{
  position:relative; height:175px;
  border-radius:10px; border:1px solid var(--stroke);
  background: rgba(255,255,255,0.03);
  overflow:hidden;
}
.tankMajorLines{ position:absolute; inset:0; pointer-events:none; }
.tankMajorLines .ml{ position:absolute; left:0; right:0; height:1px; background: var(--grid); }

/* reservoir anim */
.water{
  position:absolute; left:0; right:0; bottom:0;
  background: var(--accent); opacity:.88;
  transition: height .70s ease;
}
.marker{
  position:absolute; left:50%;
  width:0; height:0;
  border-left:7px solid transparent;
  border-right:7px solid transparent;
  border-top:10px solid var(--alarm);
  transform: translateX(-50%);
  transition: bottom .70s ease;
}

.sparkWrap{ width:100%; height:210px; margin-top:6px; }
canvas.spark{ width:100% !important; height:210px !important; }

/* ===== QC ===== */
.qcGrid{
  display:grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
  align-items: stretch;
}
@media (max-width: 1500px){ .qcGrid{ grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 1200px){ .qcGrid{ grid-template-columns: repeat(2, 1fr); } }
.qcCard{ min-height: 210px; }
.qcMeta{
  width:100%; text-align:left; margin-top:8px;
  color: var(--muted); font-weight:900;
  text-transform: uppercase; letter-spacing:.35px;
  font-size:12px; line-height:1.7;
}
.qcSparkWrap{ width:100%; height:120px; margin-top:10px; }
canvas.qcSpark{ width:100% !important; height:120px !important; }

/* ================= SLIDE VIEWPORT ================= */
.slideViewport{
  height: calc(100vh - 120px);
  overflow: hidden;
}
.slides{
  height: 100%;
  display:flex;
  width: 200%;
  transform: translateX(0%);
  transition: transform 520ms cubic-bezier(.22,.8,.23,1);
}
.slide{
  width: 50%;
  height: 100%;
  overflow: auto;
  padding-right: 8px;
}
.slide::-webkit-scrollbar { width: 10px; }
.slide::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.18); border-radius: 10px; }
body[data-theme="light"] .slide::-webkit-scrollbar-thumb { background: rgba(15,23,42,0.18); }

.slideNavBtn{
  position: fixed;
  top: 50%;
  transform: translateY(-50%);
  z-index: 9999;
  width: 46px;
  height: 46px;
  border-radius: 999px;
  border: 1px solid var(--stroke);
  background: rgba(255,255,255,0.08);
  color: var(--text);
  display:flex;
  align-items:center;
  justify-content:center;
  cursor:pointer;
  user-select:none;
  box-shadow: 0 10px 30px rgba(0,0,0,0.18);
  opacity: 0.18;
  transition: opacity .18s ease, transform .18s ease, background .18s ease;
}
.slideNavBtn .ico{
  font-size: 18px;
  font-weight: 1000;
  line-height: 1;
  transform: translateX(1px);
}
.slideNavBtn.right{ right: 14px; }
.slideNavBtn.left{ left: 14px; }
.slideNavBtn.near{ opacity: 0.62; }
.slideNavBtn:hover{
  opacity: 1.0;
  background: rgba(255,255,255,0.14);
  transform: translateY(-50%) scale(1.03);
}
body[data-theme="light"] .slideNavBtn{
  background: rgba(15,23,42,0.05);
  box-shadow: 0 10px 28px rgba(15,23,42,0.12);
}

/* ================= JADWAL PANEL (Slide QC) ================= */
.schedulePanel{ margin-top: 14px; }
.scheduleWrap{
  display:grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
@media (max-width: 1100px){
  .scheduleWrap{ grid-template-columns: 1fr; }
}
.schBox{
  border: 1px solid var(--stroke);
  border-radius: 12px;
  overflow: hidden;
  background: rgba(255,255,255,0.04);
}
body[data-theme="light"] .schBox{ background: rgba(7,20,39,0.03); }
.schHead{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--stroke);
}
.schHeadLeft{
  display:flex; align-items:center; gap:10px;
  font-weight: 1000;
  letter-spacing:.55px;
  text-transform: uppercase;
  color: var(--muted);
  font-size: 12px;
}
.badgeDot{
  width: 10px; height: 10px; border-radius: 999px;
  background: var(--accent);
  box-shadow: 0 0 0 4px rgba(45,134,255,0.12);
}
.schTableWrap{ padding: 0; }
table.sch{
  width: 100%;
  border-collapse: collapse;
  font-weight: 900;
}
table.sch thead th{
  text-align:left;
  font-size: 11px;
  letter-spacing:.45px;
  text-transform: uppercase;
  color: var(--muted);
  padding: 10px 12px;
  border-bottom: 1px solid var(--stroke);
}
table.sch tbody td{
  padding: 10px 12px;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  font-size: 12.5px;
}
body[data-theme="light"] table.sch tbody td{ border-bottom: 1px solid rgba(15,23,42,0.06); }
table.sch tbody tr:hover{
  background: rgba(255,255,255,0.06);
}
body[data-theme="light"] table.sch tbody tr:hover{
  background: rgba(7,20,39,0.04);
}
td.kode{
  font-size: 11px;
  letter-spacing:.35px;
  color: var(--muted);
}
td.lokasi{
  font-size: 11px;
  letter-spacing:.35px;
  color: var(--muted);
  text-transform: uppercase;
}
.emptyRow{
  padding: 14px 12px !important;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing:.4px;
}

/* ================= ESTIMASI CARD ================= */
.estCard{ min-height: 300px; align-items: stretch; }
.estTop{
  width:100%;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:10px;
}
.estTitleSmall{ font-size:12px; font-weight:1000; color: var(--muted); text-transform: uppercase; letter-spacing:.6px; }

.estTrend{
  display:flex; align-items:center; gap:8px;
  padding: 6px 10px;
  border:1px solid var(--stroke);
  border-radius: 999px;
  background: rgba(255,255,255,0.06);
  color: var(--muted);
  font-weight: 1000;
  text-transform: uppercase;
  letter-spacing: .35px;
  font-size: 11px;
}
body[data-theme="light"] .estTrend{ background: rgba(7,20,39,0.04); }

.trIcon{
  width: 26px; height: 26px;
  border-radius: 999px;
  border: 1px solid var(--stroke);
  display:flex; align-items:center; justify-content:center;
  background: rgba(255,255,255,0.06);
}
body[data-theme="light"] .trIcon{ background: rgba(7,20,39,0.04); }
.trIcon svg{ width: 14px; height: 14px; }
.trUp svg{ fill: var(--pos); }
.trDown svg{ fill: var(--neg); }
.trFlat svg{ fill: var(--muted); }

.estHero{
  width: 100%;
  margin-top: 10px;
  border:1px solid var(--stroke);
  background: rgba(255,255,255,0.06);
  border-radius: 12px;
  padding: 12px 12px;
  display:flex;
  flex-direction: column;
  gap: 6px;
}
body[data-theme="light"] .estHero{ background: rgba(7,20,39,0.04); }
.estHeroLabel{
  font-size: 11px;
  font-weight: 1000;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .55px;
}
.estHeroValue{
  font-size: 22px;
  font-weight: 1000;
  color: var(--text);
}

.estBar{
  width: 100%;
  height: 10px;
  border-radius: 999px;
  border:1px solid var(--stroke);
  background: rgba(255,255,255,0.06);
  overflow:hidden;
  margin-top: 10px;
}
.estFill{
  height:100%;
  width:0%;
  background: var(--accent);
  transition: width .35s ease;
}
.estMiniGrid{
  width: 100%;
  margin-top: 10px;
  display:grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.estMini{
  border: 1px solid var(--stroke);
  border-radius: 12px;
  padding: 10px 10px;
  background: rgba(255,255,255,0.04);
}
body[data-theme="light"] .estMini{ background: rgba(7,20,39,0.03); }
.estMini .k{
  font-size: 11px;
  font-weight: 1000;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing:.45px;
}
.estMini .v{
  margin-top: 6px;
  font-size: 16px;
  font-weight: 1000;
}
.estMini .sub{
  margin-top: 2px;
  font-size: 11px;
  font-weight: 1000;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing:.35px;
}
/* ETA box */
.estHero{
  width: fit-content;
  max-width: 100%;
  margin: 10px auto 0;
  padding: 12px 18px;
  text-align: center;
}
@supports not (width: fit-content){
  .estHero{ display: inline-flex; }
}
  </style>
</head>

<body data-theme="dark">
<div class="topbar">
  <div class="topbarInner">
    <img src="{{ url_for('static', filename='logo-sctk-transparan.png') }}" class="brandLogo" alt="Logo" />
    <span>WATER DISTRIBUTION PERFORMANCE & WATER QUALITY DASHBOARD</span>
  </div>

  <button id="slideToggle" class="pauseBtn" type="button" title="Pause slide" aria-label="Pause slide">
    <svg id="slideToggleIcon" viewBox="0 0 24 24" aria-hidden="true">
      <path fill="currentColor" d="M7 5h3v14H7zM14 5h3v14h-3z"/>
    </svg>
  </button>

  <button id="themeToggle" class="themeBtn" type="button">LIGHT</button>
</div>
<hr>

<div id="btnPrev" class="slideNavBtn left" title="Slide sebelumnya" aria-label="Slide sebelumnya"><span class="ico">&lt;</span></div>
<div id="btnNext" class="slideNavBtn right" title="Slide berikutnya" aria-label="Slide berikutnya"><span class="ico">&gt;</span></div>

<div class="slideViewport">
  <div id="slides" class="slides">

    <!-- ================= SLIDE 1: QC ================= -->
    <div class="slide" id="slideQC">
      <div class="sectionTitle">KUALITAS AIR (QC)</div>

      <div class="qcGrid">
        <div class="card qcCard">
          <div class="titleRow"><div class="title">KEKERUHAN</div></div>
          <div class="valueRow"><div class="big"><span id="qc_val_kekeruhan">-</span></div><div class="unit">NTU</div></div>
          <div class="qcMeta">UPDATE: <span id="qc_dt_kekeruhan">-</span></div>
          <div class="qcSparkWrap"><canvas class="qcSpark" id="qc_spark_kekeruhan"></canvas></div>
        </div>

        <div class="card qcCard">
          <div class="titleRow"><div class="title">WARNA</div></div>
          <div class="valueRow"><div class="big"><span id="qc_val_warna">-</span></div><div class="unit">TCU</div></div>
          <div class="qcMeta">UPDATE: <span id="qc_dt_warna">-</span></div>
          <div class="qcSparkWrap"><canvas class="qcSpark" id="qc_spark_warna"></canvas></div>
        </div>

        <div class="card qcCard">
          <div class="titleRow"><div class="title">PH</div></div>
          <div class="valueRow"><div class="big"><span id="qc_val_ph">-</span></div><div class="unit"></div></div>
          <div class="qcMeta">UPDATE: <span id="qc_dt_ph">-</span></div>
          <div class="qcSparkWrap"><canvas class="qcSpark" id="qc_spark_ph"></canvas></div>
        </div>

        <div class="card qcCard">
          <div class="titleRow"><div class="title">SISA CHLOR</div></div>
          <div class="valueRow"><div class="big"><span id="qc_val_sisa_chlor">-</span></div><div class="unit">MG/L</div></div>
          <div class="qcMeta">UPDATE: <span id="qc_dt_sisa_chlor">-</span></div>
          <div class="qcSparkWrap"><canvas class="qcSpark" id="qc_spark_sisa_chlor"></canvas></div>
        </div>
      </div>

      <div class="panel" style="margin-top:14px;">
        <div class="panelTitle">
          <span>GRAFIK KUALITAS AIR (QC)</span>
          <div class="panelControls">
            <select id="qcParam" class="dd">
              <option value="kekeruhan">KEKERUHAN</option>
              <option value="warna">WARNA</option>
              <option value="ph">PH</option>
              <option value="sisa_chlor">SISA CHLOR</option>
            </select>
            <select id="qcRange" class="dd">
              <option value="24">24 JAM</option>
              <option value="168">7 HARI</option>
              <option value="720" selected>30 HARI</option>
            </select>
          </div>
        </div>
        <div class="bigChartWrap"><canvas id="qcBig" class="big"></canvas></div>
        <div class="hint" id="qcHint">QC LAST UPDATE: - | CHL: -</div>
      </div>

      <!-- ================= JADWAL (Slide QC) ================= -->
      <div class="panel schedulePanel">
        <div class="panelTitle">
          <div class="panelTitleLeft">
            <div class="panelIcon" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path fill="currentColor" d="M7 2a1 1 0 0 1 1 1v1h8V3a1 1 0 1 1 2 0v1h1a3 3 0 0 1 3 3v12a3 3 0 0 1-3 3H5a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3h1V3a1 1 0 0 1 1-1Zm13 8H4v9a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-9ZM5 6a1 1 0 0 0-1 1v1h16V7a1 1 0 0 0-1-1H5Z"/>
              </svg>
            </div>
            <span>JADWAL OPERATOR & LAB</span>
          </div>

          <div class="panelControls">
            <button class="ddBtn" id="schToday" type="button">HARI INI</button>
            <button class="ddBtn" id="schTomorrow" type="button">BESOK</button>
            <input class="dd" id="schDate" type="date" />
          </div>
        </div>

        <div class="scheduleWrap">
          <div class="schBox">
            <div class="schHead">
              <div class="schHeadLeft"><span class="badgeDot"></span> OPERATOR PRODUKSI (WTP3)</div>
            </div>
            <div class="schTableWrap">
              <table class="sch">
<thead>
  <tr><th>NAMA</th><th>JAM</th><th class="lokasi">LOKASI</th></tr>
</thead>
<tbody id="schBodyOp">
  <tr><td class="emptyRow" colspan="3">- Memuat data... -</td></tr>
</tbody>
              </table>
            </div>
          </div>

          <div class="schBox">
            <div class="schHead">
              <div class="schHeadLeft"><span class="badgeDot"></span> ANALIS LABORATORIUM (LAB)</div>
            </div>
            <div class="schTableWrap">
              <table class="sch">
 <thead>
  <tr><th>NAMA</th><th>JAM</th><th class="lokasi">LOKASI</th></tr>
</thead>
<tbody id="schBodyLab">
  <tr><td class="emptyRow" colspan="3">- Memuat data... -</td></tr>
</tbody>
              </table>
            </div>
          </div>
        </div>

      </div>

    </div>

    <!-- ================= SLIDE 2: KUANTITAS ================= -->
    <div class="slide" id="slideQTY">
      <div class="sectionTitle" style="margin-top:0;">KUANTITAS (FLOW / PRESSURE)</div>

      <div class="grid">

        <div class="card">
          <div class="titleRow"><div class="title">{{ title_map["PRESSURE_DST"] }}</div></div>
          <div class="valueRow"><div class="big"><span id="val_PRESSURE_DST">{{ "%.2f"|format(data.get("PRESSURE_DST", 0.0)) }}</span></div><div class="unit">{{ unit_map["PRESSURE_DST"] }}</div></div>
          <div class="gaugeWrap" id="gaugeWrap"><canvas id="gauge_pressure" width="260" height="150"></canvas></div>
          <div class="rangeText">RANGE 0 - 5 BAR</div>
        </div>

        <div class="card">
          <div class="titleRow"><div class="title">{{ title_map["LVL_RES_WTP3"] }}</div></div>
          <div class="valueRow">
            <div class="big"><span id="val_LVL_RES_WTP3">{{ "%.2f"|format(data.get("LVL_RES_WTP3", 0.0)) }}</span></div>
            <div class="unit">{{ unit_map["LVL_RES_WTP3"] }}</div>
            <div class="unit">(<span id="pct_LVL">0</span>%)</div>
          </div>

          <div class="reservoirArea" id="reservoirArea">
            <div class="yLabels" id="lvl_labels"></div>
            <div class="tankBox">
              <div class="tankMajorLines" id="tank_major_lines"></div>
              <div class="water" id="water_level" style="height:0%"></div>
              <div class="marker" id="lvl_marker" style="bottom:0%"></div>
            </div>
          </div>
        </div>

        {% for k in display_order %}
          <div class="card">
            <div class="titleRow"><div class="title">{{ title_map.get(k, k.replace('_',' ')).upper() }}</div></div>
            <div class="valueRow"><div class="big"><span id="val_{{ k }}">{{ "%.2f"|format(data.get(k, 0.0)) }}</span></div><div class="unit">{{ unit_map.get(k, "") }}</div></div>
            <div class="sparkWrap"><canvas class="spark" id="spark_{{ k }}" data-key="{{ k }}"></canvas></div>
          </div>
        {% endfor %}

        <div class="card estCard">
          <div class="estTop">
            <div class="estTitleSmall">ESTIMASI CADANGAN AIR</div>
            <div class="estTrend" id="estTrendPill">
              <span class="trIcon trFlat" id="trendIcon">
                <svg viewBox="0 0 24 24"><path d="M4 12h16v2H4z"/></svg>
              </span>
              <span id="trendText">STABIL</span>
            </div>
          </div>

          <div class="estHero">
            <div class="estHeroLabel" id="est_eta_label">ETA</div>
            <div class="estHeroValue" id="est_eta_value">-</div>
          </div>

          <div class="estBar">
            <div class="estFill" id="est_fill" style="width:0%"></div>
          </div>

          <div class="estMiniGrid">
            <div class="estMini">
              <div class="k">LEVEL</div>
              <div class="v"><span id="est_level_m">-</span> m</div>
              <div class="sub"><span id="est_level_pct">-</span>%</div>
            </div>
            <div class="estMini">
              <div class="k">NET (SELISIH)</div>
              <div class="v"><span id="est_net_lps">-</span> L/s</div>
              <div class="sub" id="netStateSub">-</div>
            </div>
          </div>
        </div>

      </div>

      <div class="panel">
        <div class="panelTitle">
          <span>GRAFIK KUANTITAS (FLOW / PRESSURE)</span>
          <div class="panelControls">
            <select id="qtyParam" class="dd">
              <option value="TOTAL_FLOW_DST" selected>TOTAL FLOW DISTRIBUSI</option>
              <option value="TOTAL_FLOW_ITK">TOTAL FLOW INTAKE</option>
              <option value="SELISIH_FLOW">SELISIH FLOW</option>
              <option value="FLOW_WTP3">FLOW WTP 3</option>
              <option value="FLOW_50_WTP1">FLOW UPAM CIKANDE</option>
              <option value="FLOW_CIJERUK">FLOW UPAM CIJERUK</option>
              <option value="FLOW_CARENANG">FLOW UPAM CARENANG</option>
              <option value="PRESSURE_DST">PRESSURE DISTRIBUSI</option>
              <option value="LVL_RES_WTP3">LEVEL RESERVOIR WTP 3</option>
            </select>
            <select id="qtyRange" class="dd">
              <option value="1" selected>1 JAM</option>
              <option value="12">12 JAM</option>
              <option value="24">24 JAM</option>
            </select>
          </div>
        </div>
        <div class="bigChartWrap"><canvas id="chartBig" class="big"></canvas></div>
        <div class="hint" id="lastUpdate">LAST UPDATE: -</div>
      </div>
    </div>

  </div>
</div>

<script>
  const RES_MAX_M = 8.0;
  const RES_LITER_PER_M = 375000.0;

  const SLIDE_INTERVAL_MS = 10000;
  let slideIndex = 0;
  let slideTimer = null;
  let isSlidePaused = false;

  let lastQtyTsApplied = 0;
  let lastQCSigApplied = "";

  function cssVar(name){ return getComputedStyle(document.body).getPropertyValue(name).trim(); }
  function clamp(x,a,b){ return Math.max(a, Math.min(b, x)); }
  function fmt(n, d=2){
    const x = Number(n);
    if (!isFinite(x)) return "-";
    return x.toFixed(d);
  }
  function fmtTime(tsSec, withSec=false){
    const d = new Date(tsSec*1000);
    return d.toLocaleTimeString([], withSec
      ? {hour:"2-digit", minute:"2-digit", second:"2-digit"}
      : {hour:"2-digit", minute:"2-digit"}
    );
  }
  async function fetchJSON(url){
    const sep = url.includes("?") ? "&" : "?";
    const u = url + sep + "_=" + Date.now();
    const res = await fetch(u, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`);
    return await res.json();
  }

  // ===== THEME =====
  function applyTheme(theme){
    document.body.dataset.theme = theme;
    localStorage.setItem("theme", theme);
    const btn = document.getElementById("themeToggle");
    if (btn) btn.textContent = (theme === "dark") ? "LIGHT" : "DARK";
    redrawAllCharts();
  }
  function initTheme(){
    const saved = localStorage.getItem("theme");
    if (saved === "light" || saved === "dark") applyTheme(saved);
    else applyTheme("dark");
    const btn = document.getElementById("themeToggle");
    if (btn){
      btn.addEventListener("click", () => {
        applyTheme(document.body.dataset.theme === "dark" ? "light" : "dark");
      });
    }
  }
  function setChartDefaults(){
    if (!window.Chart) return;
    Chart.defaults.color = cssVar("--muted");
    Chart.defaults.borderColor = cssVar("--stroke");
    Chart.defaults.font.family = "Inter, Arial";
  }

  // ===== Slider controls =====
  function setSlide(idx){
    slideIndex = (idx % 2 + 2) % 2;
    const slides = document.getElementById("slides");
    if (!slides) return;
    slides.style.transform = `translateX(-${slideIndex * 50}%)`;
  }
  function nextSlide(){ setSlide(slideIndex + 1); }
  function prevSlide(){ setSlide(slideIndex - 1); }

  function startAutoSlide(){
    stopAutoSlide();
    if (isSlidePaused) return;
    slideTimer = setInterval(() => nextSlide(), SLIDE_INTERVAL_MS);
  }
  function stopAutoSlide(){
    if (slideTimer){ clearInterval(slideTimer); slideTimer = null; }
  }

  function setPauseUI(paused){
    const btn = document.getElementById("slideToggle");
    const ico = document.getElementById("slideToggleIcon");
    if (!btn || !ico) return;

    if (paused){
      btn.classList.add("paused");
      btn.title = "Play slide";
      btn.setAttribute("aria-label","Play slide");
      ico.innerHTML = '<path fill="currentColor" d="M8 5v14l11-7z"/>';
    } else {
      btn.classList.remove("paused");
      btn.title = "Pause slide";
      btn.setAttribute("aria-label","Pause slide");
      ico.innerHTML = '<path fill="currentColor" d="M7 5h3v14H7zM14 5h3v14h-3z"/>';
    }
  }
  function setSlidePaused(paused){
    isSlidePaused = !!paused;
    localStorage.setItem("slidePaused", isSlidePaused ? "1" : "0");
    setPauseUI(isSlidePaused);
    if (isSlidePaused) stopAutoSlide();
    else startAutoSlide();
  }
  function initSlidePause(){
    isSlidePaused = (localStorage.getItem("slidePaused") === "1");
    setPauseUI(isSlidePaused);

    const btn = document.getElementById("slideToggle");
    if (btn){
      btn.addEventListener("click", () => setSlidePaused(!isSlidePaused));
    }
  }

  function setupNavButtons(){
    const btnNext = document.getElementById("btnNext");
    const btnPrev = document.getElementById("btnPrev");

    if (btnNext) btnNext.addEventListener("click", () => { nextSlide(); if (!isSlidePaused) startAutoSlide(); });
    if (btnPrev) btnPrev.addEventListener("click", () => { prevSlide(); if (!isSlidePaused) startAutoSlide(); });

    const nearDist = 120;
    function setNear(btn, isNear){
      if (!btn) return;
      if (isNear) btn.classList.add("near");
      else btn.classList.remove("near");
    }

    document.addEventListener("mousemove", (e) => {
      for (const btn of [btnPrev, btnNext]){
        if (!btn) continue;
        const r = btn.getBoundingClientRect();
        const cx = r.left + r.width/2;
        const cy = r.top + r.height/2;
        const dx = e.clientX - cx;
        const dy = e.clientY - cy;
        const dist = Math.sqrt(dx*dx + dy*dy);
        setNear(btn, dist < nearDist);
      }
    }, { passive: true });
  }

  // ===== Reservoir labels =====
  function buildReservoirLabels(){
    const labels = document.getElementById("lvl_labels");
    const major = document.getElementById("tank_major_lines");
    if (!labels || !major) return;
    labels.innerHTML = "";
    major.innerHTML = "";
    const max = 8;
    for (let m=0; m<=max; m++){
      const pct = (m / max) * 100;
      const el = document.createElement("div");
      el.className = "yl";
      el.style.bottom = pct + "%";
      el.textContent = m + "M";
      labels.appendChild(el);

      const line = document.createElement("div");
      line.className = "ml";
      line.style.bottom = pct + "%";
      major.appendChild(line);
    }
  }

  function animateReservoirFillTo(pct){
    const water = document.getElementById("water_level");
    const marker = document.getElementById("lvl_marker");
    if (!water || !marker) return;

    water.style.transition = "none";
    marker.style.transition = "none";
    water.style.height = "0%";
    marker.style.bottom = "0%";

    requestAnimationFrame(() => {
      water.style.transition = "height .70s ease";
      marker.style.transition = "bottom .70s ease";
      water.style.height = pct + "%";
      marker.style.bottom = pct + "%";
    });
  }

  function updateReservoir(levelM, animate=true){
    const v = clamp(Number(levelM)||0, 0, RES_MAX_M);
    const pct = Math.round((v / RES_MAX_M) * 100);

    const pctEl = document.getElementById("pct_LVL");
    if (pctEl) pctEl.textContent = String(pct);

    if (animate) animateReservoirFillTo(pct);
    else {
      const water = document.getElementById("water_level");
      const marker = document.getElementById("lvl_marker");
      if (water && marker){
        water.style.height = pct + "%";
        marker.style.bottom = pct + "%";
      }
    }
  }

  // ===== Pressure Gauge =====
  let gaugeChart = null;
  function ensureGauge(){
    const canvas = document.getElementById("gauge_pressure");
    if (!canvas || !window.Chart) return null;
    const ctx = canvas.getContext("2d");
    if (gaugeChart) return gaugeChart;

    gaugeChart = new Chart(ctx, {
      type: "doughnut",
      data: { datasets: [{
        data: [0, 5],
        backgroundColor: [cssVar("--accent"), cssVar("--darkArc")],
        borderWidth: 0,
        cutout: "72%"
      }]},
      options: {
        rotation: -90,
        circumference: 180,
        responsive: true,
        maintainAspectRatio: true,
        aspectRatio: 2,
        animation: { duration: 650, easing: "easeOutQuart" },
        plugins: { legend: { display:false }, tooltip: { enabled:false } }
      }
    });
    return gaugeChart;
  }

  function updatePressureGauge(val, animate=true){
    const max = 5;
    const v = clamp(Number(val)||0, 0, max);
    const ch = ensureGauge();
    if (!ch) return;

    ch.data.datasets[0].backgroundColor = [cssVar("--accent"), cssVar("--darkArc")];

    if (!animate){
      ch.options.animation = false;
      ch.data.datasets[0].data = [v, max - v];
      ch.update();
      return;
    }

    ch.options.animation = false;
    ch.data.datasets[0].data = [0, max];
    ch.update();

    requestAnimationFrame(() => {
      ch.options.animation = { duration: 650, easing: "easeOutQuart" };
      ch.data.datasets[0].data = [v, max - v];
      ch.update();
    });
  }

  // ===== Chart helpers =====
  const POP_ANIM = { duration: 650, easing: "easeOutQuart" };
  function yFromBaseline(ctx){
    const y = ctx.chart.scales?.y;
    if(!y) return 0;
    const min = y.min;
    const max = y.max;
    let base = 0;
    if (base < min || base > max) base = min;
    return base;
  }

  // ==========================================================
  // TILE KUANTITAS (spark)
  // ==========================================================
  const QTY_TILE_SHIFT_SEC = 10;
  const QTY_TILE_POINTS    = 18;

  const qtyTileCharts = {};
  const qtyTileSeries = {};
  const qtyTileKeys = Array.from(document.querySelectorAll("canvas.spark")).map(c => c.dataset.key);

  function ensureQtySeries(key){
    if (!qtyTileSeries[key]) qtyTileSeries[key] = { labels: [], data: [], lastBucket: null };
    return qtyTileSeries[key];
  }

  function createQtyTileChart(ctx, labels, data){
    return new Chart(ctx, {
      type: "line",
      data: {
        labels: labels || [],
        datasets: [{
          data: data || [],
          borderColor: cssVar("--accent"),
          backgroundColor: cssVar("--accentFill"),
          fill: true,
          borderWidth: 2,
          tension: 0.30,
          pointRadius: 2,
          pointHoverRadius: 0,
          pointHitRadius: 0,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: POP_ANIM,
        animations: { y: { from: (ctx) => yFromBaseline(ctx) } },
        plugins: { legend: { display:false }, tooltip: { enabled:false } },
        scales: {
          x: {
            ticks: {
              color: cssVar("--muted"),
              font: { size: 10 },
              autoSkip: true,
              maxTicksLimit: 6,
              maxRotation: 0,
              minRotation: 0
            },
            title: { display: true, text: "Jam" },
            grid: { color: cssVar("--grid"), display: true }
          },
          y: {
            ticks: { color: cssVar("--muted"), font: { size: 10 }, maxTicksLimit: 6 },
            grid: { color: cssVar("--grid"), display: true }
          }
        }
      }
    });
  }

  function renderQtyTile(key){
    const canvas = document.getElementById("spark_" + key);
    if (!canvas) return;
    const s = ensureQtySeries(key);

    if (qtyTileCharts[key]) qtyTileCharts[key].destroy();
    qtyTileCharts[key] = createQtyTileChart(canvas.getContext("2d"), [...s.labels], [...s.data]);
  }

  function initQtyTileCharts(){
    for (const key of qtyTileKeys){
      ensureQtySeries(key);
      renderQtyTile(key);
    }
  }

  async function initQtyTileHistory(){
    const hours = 1;
    const interval = QTY_TILE_SHIFT_SEC;

    for (const key of qtyTileKeys){
      try{
        const arr = await fetchJSON(`/api/history/${key}?hours=${hours}&interval=${interval}&limit=${QTY_TILE_POINTS}`);
        const s = ensureQtySeries(key);
        s.labels = arr.map(p => fmtTime(p.ts, true));
        s.data   = arr.map(p => p.value);
        if (arr.length){
          s.lastBucket = Math.floor(arr[arr.length-1].ts / QTY_TILE_SHIFT_SEC) * QTY_TILE_SHIFT_SEC;
        } else {
          s.lastBucket = null;
        }
        renderQtyTile(key);
      }catch(e){
        console.log("INIT QTY TILE ERR", key, e);
      }
    }
  }

  // ===== Big chart kuantitas =====
  let qtyChart = null;
  function qtyLabel(key){
    const map = {
      "TOTAL_FLOW_DST":"TOTAL FLOW DISTRIBUSI",
      "TOTAL_FLOW_ITK":"TOTAL FLOW INTAKE",
      "SELISIH_FLOW":"SELISIH FLOW",
      "FLOW_WTP3":"FLOW WTP 3",
      "FLOW_50_WTP1":"FLOW UPAM CIKANDE",
      "FLOW_CIJERUK":"FLOW UPAM CIJERUK",
      "FLOW_CARENANG":"FLOW UPAM CARENANG",
      "PRESSURE_DST":"PRESSURE DISTRIBUSI",
      "LVL_RES_WTP3":"LEVEL RESERVOIR WTP 3"
    };
    return map[key] || key;
  }

  async function loadQtyBig(animate=true){
    const key = document.getElementById("qtyParam").value;
    const hours = Number(document.getElementById("qtyRange").value);
    const interval = (hours <= 1) ? 60 : (hours <= 12 ? 120 : 300);
    const arr = await fetchJSON(`/api/history/${key}?hours=${hours}&interval=${interval}`);

    const ctx = document.getElementById("chartBig").getContext("2d");
    if (qtyChart) qtyChart.destroy();
    qtyChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: arr.map(p => fmtTime(p.ts, false)),
        datasets: [{
          label: `${qtyLabel(key)} - ${hours} JAM`,
          data: arr.map(p => p.value),
          borderColor: cssVar("--accent"),
          backgroundColor: cssVar("--accentFill"),
          fill: true,
          pointRadius: 0,
          borderWidth: 2,
          tension: 0.30
        }]
      },
      options: {
        responsive:true,
        maintainAspectRatio:false,
        animation: animate ? POP_ANIM : false,
        animations: animate ? { y: { from: (ctx) => yFromBaseline(ctx) } } : {},
        scales: {
          x: { title: { display: true, text: "Jam" }, grid: { color: cssVar("--grid"), display:true } },
          y: { grid: { color: cssVar("--grid"), display:true } }
        }
      }
    });
  }

  // ===== QC mini sparks =====
  const qcTileKeys = ["kekeruhan","warna","ph","sisa_chlor"];
  const qcTileCharts = {};
  const qcTileSeries = {};

  function ensureQCTileSeries(k){
    if (!qcTileSeries[k]) qcTileSeries[k] = { labels: [], data: [] };
    return qcTileSeries[k];
  }

  function createQCTileChart(ctx, labels, data){
    return new Chart(ctx, {
      type: "line",
      data: { labels: labels || [], datasets: [{
        data: data || [],
        borderColor: cssVar("--accent"),
        backgroundColor: cssVar("--accentFill"),
        fill: true,
        pointRadius: 0,
        borderWidth: 2,
        tension: 0.35,
      }]},
      options: {
        responsive:true,
        maintainAspectRatio:false,
        animation: POP_ANIM,
        animations: { y: { from: (ctx) => yFromBaseline(ctx) } },
        plugins: { legend:{display:false}, tooltip:{enabled:false} },
        scales: {
          x: { grid: { color: cssVar("--grid"), display:true } },
          y: { grid: { color: cssVar("--grid"), display:true } }
        }
      }
    });
  }

  function renderQCTile(k){
    const canvas = document.getElementById("qc_spark_" + k);
    if (!canvas) return;
    const s = ensureQCTileSeries(k);

    if (qcTileCharts[k]) qcTileCharts[k].destroy();
    qcTileCharts[k] = createQCTileChart(canvas.getContext("2d"), [...s.labels], [...s.data]);
  }

  function initQCTileCharts(){
    for (const k of qcTileKeys){
      ensureQCTileSeries(k);
      renderQCTile(k);
    }
  }

  async function refreshQCTileCharts(){
    for (const k of qcTileKeys){
      try{
        const arr = await fetchJSON(`/api/qc/last/${k}?n=5`);
        const s = ensureQCTileSeries(k);
        s.labels = arr.map(p => fmtTime(p.ts, false));
        s.data   = arr.map(p => p.value);
        renderQCTile(k);
      }catch(e){
        console.log("QC TILE REFRESH ERR", k, e);
      }
    }
  }

  // ===== QC big chart =====
  let qcBigChart = null;
  function qcLabel(k){
    const map = {kekeruhan:"KEKERUHAN", warna:"WARNA", ph:"PH", sisa_chlor:"SISA CHLOR"};
    return map[k] || k;
  }
  async function loadQCBig(animate=true){
    const param = document.getElementById("qcParam").value;
    const hours = Number(document.getElementById("qcRange").value);
    const interval = (hours <= 24) ? 3600 : (hours <= 168 ? 7200 : 21600);
    const arr = await fetchJSON(`/api/qc/history/${param}?hours=${hours}&interval=${interval}`);

    const ctx = document.getElementById("qcBig").getContext("2d");
    if (qcBigChart) qcBigChart.destroy();
    qcBigChart = new Chart(ctx, {
      type:"line",
      data:{
        labels: arr.map(p => new Date(p.ts*1000).toLocaleString()),
        datasets:[{
          label: `${qcLabel(param)} - ${hours<=24 ? hours+" JAM" : (hours/24)+" HARI"}`,
          data: arr.map(p => p.value),
          borderColor: cssVar("--accent"),
          backgroundColor: cssVar("--accentFill"),
          fill:true, pointRadius:0, borderWidth:2, tension:0.30
        }]
      },
      options:{
        responsive:true,
        maintainAspectRatio:false,
        animation: animate ? POP_ANIM : false,
        animations: animate ? { y: { from: (ctx) => yFromBaseline(ctx) } } : {},
        scales: {
          x: { grid: { color: cssVar("--grid"), display:true } },
          y: { grid: { color: cssVar("--grid"), display:true } }
        }
      }
    });
  }

  // =========================
  // ESTIMASI CADANGAN
  // =========================
  function secondsToHuman(sec){
    if (sec == null || !isFinite(sec)) return "-";
    if (sec < 0) return "-";
    if (sec < 60) return Math.round(sec) + " dtk";
    const m = Math.floor(sec/60);
    const h = Math.floor(m/60);
    const mm = m % 60;
    if (h <= 0) return mm + " menit";
    return h + " jam " + mm + " menit";
  }

  function computeEtaSeconds(levelM, netLps, targetM){
    const lvl = Number(levelM);
    const net = Number(netLps);
    if (!isFinite(lvl) || !isFinite(net)) return null;
    if (Math.abs(net) < 0.2) return null;

    const rate_m_per_s = net / RES_LITER_PER_M;
    const delta = targetM - lvl;

    if ((delta > 0 && rate_m_per_s <= 0) || (delta < 0 && rate_m_per_s >= 0)) return null;
    return delta / rate_m_per_s;
  }

  function setTrendUI(net){
    const icon = document.getElementById("trendIcon");
    const text = document.getElementById("trendText");
    const sub = document.getElementById("netStateSub");

    const upSvg = '<svg viewBox="0 0 24 24"><path d="M12 4l7 7h-4v9H9v-9H5z"/></svg>';
    const dnSvg = '<svg viewBox="0 0 24 24"><path d="M12 20l-7-7h4V4h6v9h4z"/></svg>';
    const flSvg = '<svg viewBox="0 0 24 24"><path d="M4 12h16v2H4z"/></svg>';

    icon.classList.remove("trUp","trDown","trFlat");

    if (net > 0.2){
      icon.classList.add("trUp");
      icon.innerHTML = upSvg;
      if (text) text.textContent = "CADANGAN NAIK";
      if (sub) sub.textContent = "NAIK";
    } else if (net < -0.2){
      icon.classList.add("trDown");
      icon.innerHTML = dnSvg;
      if (text) text.textContent = "CADANGAN TURUN";
      if (sub) sub.textContent = "TURUN";
    } else {
      icon.classList.add("trFlat");
      icon.innerHTML = flSvg;
      if (text) text.textContent = "STABIL";
      if (sub) sub.textContent = "STABIL";
    }
  }

  function updateEstimationUI(levelM, netLps){
    const lvl = clamp(Number(levelM)||0, 0, RES_MAX_M);
    const net = Number(netLps)||0;
    const pct = Math.round((lvl / RES_MAX_M) * 100);

    document.getElementById("est_level_m").textContent = fmt(lvl, 2);
    document.getElementById("est_level_pct").textContent = String(pct);
    document.getElementById("est_net_lps").textContent = fmt(net, 2);
    document.getElementById("est_fill").style.width = pct + "%";

    setTrendUI(net);

    const etaLabel = document.getElementById("est_eta_label");
    const etaValue = document.getElementById("est_eta_value");

    if (Math.abs(net) < 0.2){
      if (etaLabel) etaLabel.textContent = "ETA";
      if (etaValue) etaValue.textContent = "- (net hampir nol)";
      return;
    }

    if (net < -0.2){
      const eta1 = computeEtaSeconds(lvl, net, 1.0);
      if (etaLabel) etaLabel.textContent = "ETA ke 1m";
      if (etaValue) etaValue.textContent = (eta1 == null) ? "- (tidak menuju 1m)" : secondsToHuman(eta1);
    } else {
      const eta8 = computeEtaSeconds(lvl, net, 8.0);
      if (etaLabel) etaLabel.textContent = "ETA ke 100%";
      if (etaValue) etaValue.textContent = (eta8 == null) ? "- (tidak menuju penuh)" : secondsToHuman(eta8);
    }
  }

  function applySelisihColor(v){
    const el = document.getElementById("val_SELISIH_FLOW");
    if (!el) return;
    el.classList.remove("valPos","valNeg");
    const n = Number(v);
    if (!isFinite(n)) return;
    if (n > 0) el.classList.add("valPos");
    else if (n < 0) el.classList.add("valNeg");
  }

  // ===== Apply data to UI =====
  function applyQty(payload){
    if (!payload) return;
    const ts = payload.ts || 0;
    const data = payload.data || {};

    const isNew = (ts && ts > lastQtyTsApplied);

    if (ts) {
      document.getElementById("lastUpdate").textContent =
        "LAST UPDATE: " + new Date(ts*1000).toLocaleString();
    }

    for (const [k,v] of Object.entries(data)){
      const id = "val_" + k;
      const el = document.getElementById(id);
      if (el) el.textContent = fmt(v, 2);
    }

    applySelisihColor(data.SELISIH_FLOW);

    updateReservoir(data.LVL_RES_WTP3, isNew);
    updatePressureGauge(data.PRESSURE_DST, isNew);

    updateEstimationUI(data.LVL_RES_WTP3, data.SELISIH_FLOW);

    if (ts){
      const bucket = Math.floor(ts / 10) * 10;
      const label = fmtTime(bucket, true);

      for (const key of qtyTileKeys){
        const v = Number(data[key]);
        if(!isFinite(v)) continue;

        const s = ensureQtySeries(key);
        const isNewBucket = (s.lastBucket !== bucket);

        if (isNewBucket){
          s.labels.push(label);
          s.data.push(v);
          s.lastBucket = bucket;
          while (s.labels.length > 18){
            s.labels.shift();
            s.data.shift();
          }
        } else {
          if (s.data.length) s.data[s.data.length - 1] = v;
          else { s.labels.push(label); s.data.push(v); }
        }

        const ch = qtyTileCharts[key];
        if (ch){
          ch.options.animation = isNewBucket ? POP_ANIM : { duration: 180, easing: "linear" };
          ch.data.labels = [...s.labels];
          ch.data.datasets[0].data = [...s.data];
          ch.update();
        } else {
          renderQtyTile(key);
        }
      }
    }

    if (isNew){
      loadQtyBig(true);
      lastQtyTsApplied = ts;
    }
  }

  function applyQC(payload){
    if (!payload) return;

    document.getElementById("qcHint").textContent =
      `QC LAST UPDATE: ${payload.qc_last_update || "-"} | CHL: ${payload.chlor_last_update || "-"}`;

    const sig = JSON.stringify({
      u: payload.qc_last_update,
      c: payload.chlor_last_update,
      k: payload.latest?.kekeruhan?.value,
      s: payload.latest?.sisa_chlor?.value
    });
    const isNewQC = (sig !== lastQCSigApplied);

    for (const k of qcTileKeys){
      const vEl = document.getElementById("qc_val_" + k);
      const dEl = document.getElementById("qc_dt_" + k);
      const obj = (payload.latest && payload.latest[k]) ? payload.latest[k] : null;

      const val = (obj && obj.value != null) ? obj.value : null;
      if (vEl){
        vEl.textContent = (val == null) ? "-" : fmt(val, 2);
      }
      if (dEl) dEl.textContent = (obj && obj.dt) ? obj.dt : "-";
    }

    if (isNewQC){
      lastQCSigApplied = sig;
      refreshQCTileCharts();
      loadQCBig(true);
    }
  }

  // ===== JADWAL (Slide QC) =====
  function ymd(d){
    const pad = (n)=> String(n).padStart(2,"0");
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
  }
  function setSchDateInput(val){
    const inp = document.getElementById("schDate");
    if (inp) inp.value = val;
  }
  function renderScheduleRows(tbodyId, rows){
    const tb = document.getElementById(tbodyId);
    if (!tb) return;
    tb.innerHTML = "";
    if (!rows || !rows.length){
      // >>>>>>>>>>>> FIX: colspan harus 3 (bukan 4) <<<<<<<<<<<<
      tb.innerHTML = `<tr><td class="emptyRow" colspan="3">- TIDAK ADA DATA -</td></tr>`;
      return;
    }
    for (const r of rows){
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${(r.nama||"-")}</td>
        <td>${(r.jam||"-")}</td>
        <td class="lokasi">${(r.lokasi||"-")}</td>
      `;
      tb.appendChild(tr);
    }
  }
  async function loadSchedule(dateStr){
    try{
      const j = await fetchJSON(`/api/schedule?date=${encodeURIComponent(dateStr)}`);
      renderScheduleRows("schBodyOp", j.operator || []);
      renderScheduleRows("schBodyLab", j.lab || []);
    }catch(e){
      renderScheduleRows("schBodyOp", []);
      renderScheduleRows("schBodyLab", []);
      console.log("SCHEDULE LOAD ERR", e);
    }
  }
  function initSchedule(){
    const today = new Date();
    const d0 = ymd(today);
    setSchDateInput(d0);
    loadSchedule(d0);

    const btnT = document.getElementById("schToday");
    const btnB = document.getElementById("schTomorrow");
    const inp  = document.getElementById("schDate");

    if (btnT) btnT.addEventListener("click", () => {
      const d = ymd(new Date());
      setSchDateInput(d);
      loadSchedule(d);
    });
    if (btnB) btnB.addEventListener("click", () => {
      const x = new Date();
      x.setDate(x.getDate()+1);
      const d = ymd(x);
      setSchDateInput(d);
      loadSchedule(d);
    });
    if (inp) inp.addEventListener("change", () => {
      const d = inp.value;
      if (d) loadSchedule(d);
    });

    // refresh tiap 5 menit
    setInterval(() => {
      const d = (document.getElementById("schDate")?.value) || ymd(new Date());
      loadSchedule(d);
    }, 300000);
  }

  // ===== SSE =====
  function startSSE(){
    try{
      const es = new EventSource("/events");
      es.onmessage = async (ev) => {
        try{
          const j = JSON.parse(ev.data);
          if (j.qty) applyQty(j.qty);
          if (j.qc) applyQC(j.qc);
        }catch(e){
          console.log("SSE parse/apply error", e);
        }
      };
      es.onerror = () => {
        console.log("SSE error, fallback polling...");
        es.close();
        startPolling();
      };
      return true;
    }catch(e){
      console.log("SSE not available", e);
      return false;
    }
  }

  // ===== Polling fallback =====
  let pollTimer = null;
  function startPolling(){
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      try{
        const qty = await fetchJSON("/api/latest");
        applyQty(qty);
      }catch(e){}

      try{
        const qc = await fetchJSON("/api/qc/latest");
        applyQC(qc);
      }catch(e){}
    }, 5000);
  }

  function redrawAllCharts(){
    setChartDefaults();

    for (const key of qtyTileKeys){
      if (qtyTileCharts[key]) qtyTileCharts[key].destroy();
      qtyTileCharts[key] = null;
      renderQtyTile(key);
    }

    for (const k of qcTileKeys){
      if (qcTileCharts[k]) qcTileCharts[k].destroy();
      qcTileCharts[k] = null;
      renderQCTile(k);
    }

    const p = parseFloat(document.getElementById("val_PRESSURE_DST")?.textContent || "0") || 0;
    const lvl = parseFloat(document.getElementById("val_LVL_RES_WTP3")?.textContent || "0") || 0;
    updatePressureGauge(p, false);
    updateReservoir(lvl, false);

    loadQtyBig(false);
    loadQCBig(false);
  }

  (async function(){
    try{
      initTheme();
      setChartDefaults();
      buildReservoirLabels();

      setSlide(0);
      initSlidePause();
      setupNavButtons();
      startAutoSlide();

      initQtyTileCharts();
      initQCTileCharts();

      await initQtyTileHistory();
      await refreshQCTileCharts();

      await loadQtyBig(false);
      await loadQCBig(false);

      document.getElementById("qtyParam").addEventListener("change", () => loadQtyBig(true));
      document.getElementById("qtyRange").addEventListener("change", () => loadQtyBig(true));
      document.getElementById("qcParam").addEventListener("change", () => loadQCBig(true));
      document.getElementById("qcRange").addEventListener("change", () => loadQCBig(true));

      try{ applyQty(await fetchJSON("/api/latest")); }catch(e){}
      try{ applyQC(await fetchJSON("/api/qc/latest")); }catch(e){}

      initSchedule();

      if (!startSSE()) startPolling();

    }catch(e){
      console.log("INIT FATAL", e);
      startPolling();
    }
  })();
</script>
</body>
</html>
"""

@app.after_request
def add_no_cache_headers(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/")
def index():
    with data_lock:
        data = dict(latest_data)
    return render_template_string(
        HTML_PAGE,
        data=data,
        title_map=TITLE_MAP,
        unit_map=UNIT_MAP,
        display_order=DISPLAY_ORDER,
    )

# ===== API kuantitas =====
@app.route("/api/latest")
def api_latest():
    with data_lock:
        data = dict(latest_data)
        ts = int(latest_ts_epoch or time.time())
    return jsonify({"ts": ts, "data": data})

@app.route("/api/history/<key>")
def api_history(key):
    hours = float(request.args.get("hours", 24))
    interval = int(request.args.get("interval", 60))
    now = int(time.time())
    start = now - int(hours * 3600)

    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                (CAST(ts / ? AS INTEGER) * ?) AS bucket,
                AVG(value) AS avg_value
            FROM measurements
            WHERE key = ? AND ts >= ?
            GROUP BY bucket
            ORDER BY bucket
        """, (interval, interval, key, start))
        rows = cur.fetchall()

    out = [{"ts": int(r[0]), "value": float(r[1])} for r in rows]

    limit = request.args.get("limit")
    if limit:
        try:
            n = max(1, int(limit))
            out = out[-n:]
        except:
            pass

    return jsonify(out)

# ===== API QC =====
@app.route("/api/qc/latest")
def api_qc_latest():
    with qc_lock:
        payload = {
            "ts": int(time.time()),
            "qc_last_update": qc_last_update_dt,
            "chlor_last_update": qc_last_update_chlor_dt,
            "latest": qc_latest,
            "status": qc_status,
        }
    return jsonify(payload)

@app.route("/api/qc/history/<param>")
def api_qc_history(param):
    hours = float(request.args.get("hours", 24))
    interval = int(request.args.get("interval", 3600))
    return jsonify(qc_history(param, hours=hours, interval=interval))

@app.route("/api/qc/last/<param>")
def api_qc_last(param):
    n = int(request.args.get("n", 5))
    if param not in QC_PARAMS:
        return jsonify([])

    with qc_lock:
        rows = list(qc_rows)

    out = []
    for rr in reversed(rows):
        v = rr.get(param)
        if v is None:
            continue
        out.append({"ts": rr["ts"], "value": float(v)})
        if len(out) >= n:
            break

    out.reverse()
    return jsonify(out)

# ===== API JADWAL =====
@app.route("/api/schedule")
def api_schedule():
    date_str = (request.args.get("date") or "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    op, lab = _schedule_for_date(date_str)

    with schedule_lock:
        meta = {
            "loaded_at": schedule_last_loaded,
            "error": schedule_last_error,
            "file": SCHEDULE_JSON_FILE,
        }

    return jsonify({
        "date": date_str,
        "operator": op,
        "lab": lab,
        "meta": meta
    })

# ===== SSE stream =====
@app.route("/events")
def events():
    def gen():
        last_qc_sent = None
        last_qty_sent = None
        while True:
            time.sleep(2)

            with data_lock:
                qty = {"ts": int(latest_ts_epoch or time.time()), "data": dict(latest_data)}
            with qc_lock:
                qc = {
                    "ts": int(time.time()),
                    "qc_last_update": qc_last_update_dt,
                    "chlor_last_update": qc_last_update_chlor_dt,
                    "latest": dict(qc_latest),
                    "status": dict(qc_status),
                }

            qty_sig = (qty["ts"], qty["data"].get("TOTAL_FLOW_DST"), qty["data"].get("PRESSURE_DST"), qty["data"].get("LVL_RES_WTP3"))
            qc_sig = (qc.get("qc_last_update"), qc.get("chlor_last_update"),
                      qc["latest"].get("kekeruhan", {}).get("value"),
                      qc["latest"].get("sisa_chlor", {}).get("value"))

            send_qty = (qty_sig != last_qty_sent)
            send_qc = (qc_sig != last_qc_sent)

            if send_qty or send_qc:
                msg = {}
                if send_qty:
                    msg["qty"] = qty
                    last_qty_sent = qty_sig
                if send_qc:
                    msg["qc"] = qc
                    last_qc_sent = qc_sig

                yield f"data: {json.dumps(msg)}\n\n"

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(gen(), headers=headers)

# ================== MQTT ==================
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to MQTT broker")
        client.subscribe(TOPIC, qos=0)
        client.subscribe(TOPIC + "/#", qos=0)
    else:
        print("Failed to connect to MQTT, code:", rc)

def on_message(client, userdata, msg):
    global last_send_time, latest_ts_epoch
    try:
        payload_text = msg.payload.decode(errors="ignore").strip()
        if not payload_text:
            return

        raw = json.loads(payload_text)

        if isinstance(raw, dict):
            if "data" in raw and isinstance(raw["data"], dict):
                raw = raw["data"]
            elif "payload" in raw and isinstance(raw["payload"], dict):
                raw = raw["payload"]
            elif "payload" in raw and isinstance(raw["payload"], str):
                try:
                    j2 = json.loads(raw["payload"])
                    if isinstance(j2, dict):
                        raw = j2
                except:
                    pass

        if not isinstance(raw, dict):
            return

        raw_u = {str(k).upper(): v for k, v in raw.items()}

        with data_lock:
            prev = dict(latest_data)

        data = {}
        matched = 0

        for key in NUMERIC_KEYS:
            if key in raw_u:
                v = raw_u.get(key)
                try:
                    if isinstance(v, str):
                        v = v.strip().replace(",", ".")
                    data[key] = float(v)
                    matched += 1
                except:
                    data[key] = float(prev.get(key, 0.0))
            else:
                data[key] = float(prev.get(key, 0.0))

        if matched == 0:
            return

        data["SELISIH_FLOW"] = float(data.get("TOTAL_FLOW_ITK", 0.0)) - float(data.get("TOTAL_FLOW_DST", 0.0))

        with data_lock:
            latest_data.clear()
            latest_data.update(data)
            latest_ts_epoch = int(time.time())

        save_to_db(latest_ts_epoch, data)

        now = time.time()
        if now - last_send_time >= SEND_INTERVAL:
            try:
                requests.post(
                    WEB_APP_URL,
                    headers={"Content-Type": "application/json"},
                    data=json.dumps(data),
                    timeout=10
                )
            except Exception as e:
                print("HTTP post error:", e)
            last_send_time = now

    except Exception as e:
        print("MQTT processing error:", e)

def mqtt_thread():
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 60)
    client.loop_forever()

# ================== MAIN ==================
if __name__ == "__main__":
    init_db()
    threading.Thread(target=mqtt_thread, daemon=True).start()
    threading.Thread(target=qc_worker, daemon=True).start()
    threading.Thread(target=schedule_worker, daemon=True).start()

    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


