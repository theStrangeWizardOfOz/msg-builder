from flask import Flask, render_template, request, redirect, url_for, flash, session
import re, math, platform, os, threading, webbrowser
from datetime import datetime

app = Flask(__name__)
# flash 메시지용
app.secret_key = "dev"

# Telex 수신처
DEFAULT_TO_NRT = {
    "to01": "ICNODOZ",
    "to02": "NRTOJNH",
    "to03": "NRTKKOZ",
    "to04": "NRTFFOZ",
    "to05": "CDGCSXH",
    "to06": "",
}

DEFAULT_TO_HND = {
    "to01": "HNDFFNH",
    "to02": "HNDKKNH",
    "to03": "HNDOINH",
    "to04": "HNDAGNH",
    "to05": "NRTFFOZ",
    "to06": "",
}

# ===== Parser =====

def parse_afocs_text(txt: str):
    """
    # 지정된 afocs txt에서 필요한 정보 파싱
    - FLT 5문자
    - LCL 날짜 치환 2025-08-30 > ddMMM)
    - DEP/ARR 3 Letter
    - REG 4자리
    - SHIP Type
    - PAX C, Y, T 각각 3자리
    """
    # 라인 전체에서 탐지
    # 편명
    m_flt = re.search(r"\b([A-Z]{2}\d{3,4})\b", txt)
    flt = m_flt.group(1) if m_flt else "UNKNOWN"
    
    # 2) LCL DATE: "(1ST LEG LCL DATE)" 라인이 포함된 곳의 날짜(YYYY-MM-DD) → ddMMM
    m_lcl = re.search(r"\b(\d{4}-\d{2}-\d{2})\b(?=.*1ST LEG LCL DATE)", txt, re.IGNORECASE | re.DOTALL)
    ddmmm = "??"
    if m_lcl:
        try:
            dt = datetime.strptime(m_lcl.group(1), "%Y-%m-%d")
            ddmmm = dt.strftime("%d%b").upper()
        except:
            pass
            
    # 3) DEP/ARR/REG/SHIP: "NRT/ICN 7741 333 ..." 형태
    # 공백이 많타
    m_route = re.search(r"\b([A-Z]{3})\s*/\s*([A-Z]{3})\b\s+(\d+)\s+(\d{2,3}[A-Z]?)", txt)
    dep = m_route.group(1) if m_route else "???"
    arr = m_route.group(2) if m_route else "???"
    reg = m_route.group(3) if m_route else "????"
    ship_raw = m_route.group(4) if m_route else "???"

    # 4) PAX 구간 에서 C,Y,T 추출
    m_pax = re.search(r"PAX\s+F(\d+)-C(\d+)-Y(\d+)-T(\d+)", txt)
    pax_c = int(m_pax.group(2)) if m_pax else 0
    pax_y = int(m_pax.group(3)) if m_pax else 0
    pax_t = int(m_pax.group(4)) if m_pax else 0

    return {
        "FLT": flt,
        "ddMMM": ddmmm,
        "DEP": dep,
        "ARR": arr,
        "REG": reg,
        "SHIP_RAW": ship_raw,
        "PAXC": pax_c,
        "PAXY": pax_y,
        "PAXT": pax_t,
    }

def normalize_ship(ship_raw: str) -> str:
    """
    # SHIP 표시 규칙
    - 3으로 시작 → A###
    - 7로 시작 → B###
    """
    s = ship_raw.strip().upper()
    if s.startswith("3"):
        return f"A{s}"
    if s.startswith("7"):
        return f"B{s}"
    return s

def calc_bag_text(ship_raw: str, pax_t: int, bag_type_input: str, ratio_ake: int, ratio_akh: int):
    """
    # BAG 규칙
    - 유저가 'AKE' 선택 → ceil(T / 40) + 'LD3'
    - 유저가 'ALF' 선택 → ceil(T / 80) + 'LDF'
    - ship_raw in {'321','32Q'} → 강제 'LD3-45', ceil(T/30) + 'LD3-45' (유저 선택 무시)
    """
    ship_key = ship_raw.strip().upper()
    force_akh = ship_key in {"321", "32Q"}

    if force_akh:
        count = math.ceil(pax_t / ratio_akh) if pax_t > 0 else 0
        return f"{count}LD3-45", "AKH (forced)"

    bt = (bag_type_input or "").strip().upper()
    if bt == "AKE":
        count = math.ceil(pax_t / ratio_ake) if pax_t > 0 else 0
        return f"{count}LD3", "AKE"
    elif bt == "ALF":
        count = math.ceil(pax_t / 80) if pax_t > 0 else 0
        return f"{count}LD6", "ALF"
    elif bt == "AKH":
        count = math.ceil(pax_t / ratio_akh) if pax_t > 0 else 0
        return f"{count}LD3-45", "AKH"
    # 기본값
    #else:
    #    count = math.ceil(pax_t / ratio_ake) if pax_t > 0 else 0
    #    return f"{count}LD3", "AKE (default)"

# 화물이 BAG랑 같은 ULD Type이면 합침
def merge_ldp(ldp: str):
    parts = re.findall(r"(\d+)(LD3(?:-45)?|LD6)", ldp.upper())
    merged = {}
    for num, typ in parts:
        merged[typ] = merged.get(typ, 0) + int(num)
    return " ".join(f"{v}{k}" for k, v in merged.items())

def build_telex_text(meta, to_vals, wgt, ldp, name, bag_text, ship_norm):
    to01 = to_vals.get("to01", "").strip()
    to02 = to_vals.get("to02", "").strip()
    to03 = to_vals.get("to03", "").strip()
    to04 = to_vals.get("to04", "").strip()
    to05 = to_vals.get("to05", "").strip()
    to06 = to_vals.get("to06", "").strip()
    
    # 화물이 NIL 이면 TTL 집계 생략
    if ldp.upper() == "NIL":
        ttl_text = bag_text
    elif re.fullmatch(r"(\d+LD3(?:-45)?\s*)+|(\d+LD6\s*)+", ldp.upper()):
        ttl_text = merge_ldp(f"{bag_text} {ldp}")
    else:
        ttl_text = f"{bag_text} {ldp}".strip()

    # Telex 양식
    out = []
    out.append(f"QD {to01} {to02} {to03} {to04} {to05} {to06}".rstrip())
    out.append(f".NRTFFOZ") # 수정 금지. 보내는 사람 주소.
    out.append(f"ADD INFO {meta['FLT']}/{meta['ddMMM']} {meta['DEP']}/{meta['ARR']} WT:KGS")
    out.append(f"AA.  SHIP : HL {meta['REG']} ({ship_norm})")
    out.append(f"BB.  PAX : C-{meta['PAXC']:03d} Y-{meta['PAXY']:03d}")
    out.append(f"CC.  CGO : {wgt} KG")
    out.append(f"DD.  ULD : BAG : {bag_text}")
    out.append(f"           CGO : {ldp}")
    out.append(f"           TTL : {ttl_text}")
    out.append(f"GTTL {wgt}/{name}")
    return "\n".join(out) + "\n"
# ===== 파서 퇴장 =====

# 라우팅
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST" and request.form.get("preset") in ("NRT", "HND"):
        session["preset"] = request.form["preset"]
        session.pop("form_data", None)
        flash(f"{session['preset']} 프리셋을 적용했습니다.", "ok")
        return redirect(url_for("index"))

    preset = session.get("preset", "NRT")
    defaults = DEFAULT_TO_NRT if preset == "NRT" else DEFAULT_TO_HND
    form_data = session.get("form_data", {})
    return render_template("index.html", defaults=defaults, form_data=form_data, result_text="")

@app.route("/process", methods=["POST"])
def process():
    afocs_text = request.form.get("afocs_text", "").strip()
    if not afocs_text:
        flash("AFOCS 텍스트를 입력하세요.", "error")
        return redirect(url_for("index"))

    to_vals = {f"to0{i}": request.form.get(f"to0{i}", "") for i in range(1, 7)}
    wgt = request.form.get("wgt", "").strip()
    ldp = request.form.get("ldp", "").strip()
    name = request.form.get("name", "").strip()
    # BAG 소수점 = 올림 처리
    bag_type = request.form.get("bag_type", "AKE").strip().upper()
    ratio_ake = int(request.form.get("bag_ratio_ake", "40"))
    ratio_akh = int(request.form.get("bag_ratio_akh", "30"))

    session["form_data"] = {
        "to_vals": to_vals,
        "wgt": wgt,
        "ldp": ldp,
        "name": name,
        "bag_type": bag_type,
        "bag_ratio_ake": ratio_ake,
        "bag_ratio_akh": ratio_akh,
        "afocs_text": afocs_text,
    }

    meta = parse_afocs_text(afocs_text)
    ship_norm = normalize_ship(meta["SHIP_RAW"])
    bag_text, _ = calc_bag_text(meta["SHIP_RAW"], meta["PAXT"], bag_type, ratio_ake, ratio_akh)
    result = build_telex_text(meta, to_vals, wgt, ldp, name, bag_text, ship_norm)

    flash("처리 완료. 결과가 아래에 표시됩니다.", "ok")
    preset = session.get("preset", "NRT")
    defaults = DEFAULT_TO_NRT if preset == "NRT" else DEFAULT_TO_HND
    form_data = session.get("form_data", {})
    return render_template("index.html", defaults=defaults, form_data=form_data, result_text=result)

def open_browser():
    webbrowser.open("http://127.0.0.1:988/")

if __name__ == "__main__":
    threading.Timer(0.8, open_browser).start()
    app.run(host="127.0.0.1", port=988, debug=True, use_reloader=False)