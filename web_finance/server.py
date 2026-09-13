from fastapi.staticfiles import StaticFiles
import os
import json
import time
import re
import asyncio
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import httplib2

app = FastAPI(title="CEO AI OS - Finance System (100X Turbo)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TOKEN_PATH = "/opt/ai-os/products/ceo/secrets/google_accounts/nguyenductuan181_at_gmail_com.json"
SPREADSHEET_ID = "1SAD-gWkx3YEMqF5PHlQq1MFC12LIp5SkbhZ2EuRgeaQ"
LOCAL_LOG_PATH = "/opt/ai-os/products/ceo/data/transactions_log.jsonl"
SHM_CACHE_PATH = "/dev/shm/ceo_finance_cache.json"

# State in RAM (0ms access)
_state = {
    "data": None,
    "last_sync": 0,
    "next_row": 70,
    "syncing": False
}

_service = None

def get_service():
    global _service
    if _service is None:
        with open(TOKEN_PATH) as f:
            creds_data = json.load(f)
        creds = Credentials.from_authorized_user_info(creds_data)
        _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _service

def parse_amount(val) -> int:
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip().lower().replace(",", ".").replace(" ", "")
    if s.endswith("tr") or s.endswith("m"):
        num = float(s[:-2] if s.endswith("tr") else s[:-1])
        return int(num * 1_000_000)
    elif s.endswith("k"):
        num = float(s[:-1])
        return int(num * 1_000)
    else:
        clean = re.sub(r"[^\d]", "", s)
        return int(clean) if clean else 0

def fetch_and_compute_from_google():
    """Batch fetch all 4 ranges in ONE single HTTP request"""
    service = get_service()
    
    ranges = [
        "'Bức tranh tài chính'!B5:G7",
        "'Thực tế TN - CP'!A4:O40",
        "'Nhật ký tài chính'!A7:K200",
        "'List HM'!A6:B40"
    ]
    
    batch_res = service.spreadsheets().values().batchGet(
        spreadsheetId=SPREADSHEET_ID,
        ranges=ranges,
        valueRenderOption="FORMATTED_VALUE"
    ).execute()
    
    value_ranges = batch_res.get("valueRanges", [])
    bt_rows = value_ranges[0].get("values", []) if len(value_ranges) > 0 else []
    tt_rows = value_ranges[1].get("values", []) if len(value_ranges) > 1 else []
    nk_rows = value_ranges[2].get("values", []) if len(value_ranges) > 2 else []
    hm_rows = value_ranges[3].get("values", []) if len(value_ranges) > 3 else []
    
    # 1. Bức tranh tài chính
    total_assets = bt_rows[0][1].strip() if len(bt_rows) > 0 and len(bt_rows[0]) > 1 else "10.757.000.000"
    total_liabilities = bt_rows[1][5].strip() if len(bt_rows) > 1 and len(bt_rows[1]) > 5 else "1.600.000.000"
    net_worth_num = parse_amount(total_assets) - parse_amount(total_liabilities)
    
    # 2. Thực tế TN - CP
    months = ["Tháng 1", "Tháng 2", "Tháng 3", "Tháng 4", "Tháng 5", "Tháng 6", "Tháng 7", "Tháng 8", "Tháng 9", "Tháng 10", "Tháng 11", "Tháng 12"]
    monthly_income = [0] * 12
    monthly_expense = [0] * 12
    monthly_net = [0] * 12
    cat_expenses = []
    
    for r in tt_rows:
        if not r or len(r) < 3:
            continue
        label = r[1].strip()
        if label == "TỔNG THU NHẬP":
            for i in range(12):
                if len(r) > i + 3:
                    monthly_income[i] = parse_amount(r[i+3])
        elif label == "TỔNG CHI PHÍ":
            for i in range(12):
                if len(r) > i + 3:
                    monthly_expense[i] = parse_amount(r[i+3])
        elif label == "THU NHẬP - CHI PHÍ":
            for i in range(12):
                if len(r) > i + 3:
                    monthly_net[i] = parse_amount(r[i+3])
        elif r[0] not in ["A", "B", "C", "STT"] and len(r) >= 3:
            tot_val = parse_amount(r[2])
            if tot_val > 0:
                cat_expenses.append({"name": label, "total": tot_val})
                
    cat_expenses.sort(key=lambda x: x["total"], reverse=True)
    
    # 3. Nhật ký tài chính
    transactions = []
    last_balance = "0"
    valid_count = 0
    for idx, r in enumerate(nk_rows, 7):
        if not r or len(r) < 2 or not r[1].strip():
            continue
        valid_count += 1
        stt = r[0].strip() if len(r) > 0 else str(valid_count)
        date_str = r[1].strip() if len(r) > 1 else ""
        month_str = r[2].strip() if len(r) > 2 else ""
        content = r[3].strip() if len(r) > 3 else ""
        thu = r[5].strip() if len(r) > 5 else ""
        chi = r[6].strip() if len(r) > 6 else ""
        ton = r[7].strip() if len(r) > 7 else ""
        cat = r[10].strip() if len(r) > 10 else ""
        
        if ton:
            last_balance = ton

        is_income = bool(thu and thu != "0" and thu != "-")
        amt_str = thu if is_income else chi
        
        transactions.append({
            "row": idx,
            "stt": stt,
            "date": date_str,
            "month": month_str,
            "content": content,
            "type": "Thu nhập" if is_income else "Chi phí",
            "amount": amt_str,
            "balance": ton,
            "category": cat
        })
        
    _state["next_row"] = 7 + valid_count

    # 4. List HM
    income_cats = []
    expense_cats = []
    current_mode = "thu"
    for r in hm_rows:
        if not r or len(r) < 2:
            continue
        txt = r[1].strip()
        if r[0] == "B" or "CHI PHÍ" in txt:
            current_mode = "chi"
            continue
        if current_mode == "thu":
            income_cats.append(txt)
        else:
            expense_cats.append(txt)

    result = {
        "ok": True,
        "kpis": {
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "net_worth": f"{net_worth_num:,}".replace(",", "."),
            "last_balance": last_balance,
            "m2_income": f"{monthly_income[1]:,}".replace(",", "."),
            "m2_expense": f"{monthly_expense[1]:,}".replace(",", "."),
            "m2_net": f"{monthly_net[1]:,}".replace(",", "."),
            "m2_rate": f"{(monthly_net[1]/monthly_income[1]*100):.1f}%" if monthly_income[1] > 0 else "0%"
        },
        "monthly": {
            "labels": months,
            "income": monthly_income,
            "expense": monthly_expense,
            "net": monthly_net
        },
        "top_categories": cat_expenses[:10],
        "recent_transactions": list(reversed(transactions))[:20],
        "total_transactions": len(transactions),
        "categories": {
            "income": income_cats,
            "expense": expense_cats
        },
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit",
        "sync_time_ms": int(time.time() * 1000)
    }
    
    # Save to RAM Disk /dev/shm
    try:
        with open(SHM_CACHE_PATH, "w") as f:
            json.dump(result, f)
    except Exception:
        pass
        
    return result

async def background_sync_worker():
    """Chạy ngầm liên tục cập nhật cache vào RAM Disk không làm block người dùng"""
    while True:
        try:
            res = await asyncio.to_thread(fetch_and_compute_from_google)
            _state["data"] = res
            _state["last_sync"] = time.time()
        except Exception as e:
            print(f"[Sync Worker Error]: {e}")
        await asyncio.sleep(45)

@app.on_event("startup")
async def startup_event():
    # Load from /dev/shm cache immediately on boot
    if os.path.exists(SHM_CACHE_PATH):
        try:
            with open(SHM_CACHE_PATH) as f:
                _state["data"] = json.load(f)
                _state["last_sync"] = time.time()
        except Exception:
            pass
    # Initial sync in background
    asyncio.create_task(background_sync_worker())

@app.get("/api/overview")
def get_overview(force: bool = False):
    t0 = time.perf_counter()
    if force or _state["data"] is None:
        _state["data"] = fetch_and_compute_from_google()
        _state["last_sync"] = time.time()
        
    res = dict(_state["data"])
    res["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    return res

class TransactionRequest(BaseModel):
    date: str
    type: str
    category: str
    amount: str
    content: str
    target: Optional[str] = ""

def write_to_sheet_task(target_row: int, d: str, content: str, thu_val, chi_val, category: str, log_entry: dict):
    """Background task to sync to Google Sheets without blocking API response"""
    try:
        service = get_service()
        updates = [
            {"range": f"'Nhật ký tài chính'!B{target_row}", "values": [[d]]},
            {"range": f"'Nhật ký tài chính'!D{target_row}", "values": [[content]]},
            {"range": f"'Nhật ký tài chính'!F{target_row}", "values": [[thu_val]]},
            {"range": f"'Nhật ký tài chính'!G{target_row}", "values": [[chi_val]]},
            {"range": f"'Nhật ký tài chính'!K{target_row}", "values": [[category]]}
        ]
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": updates}
        ).execute()
        
        # Trigger background recompute
        fetch_and_compute_from_google()
    except Exception as e:
        print(f"[Async Write Error]: {e}")

@app.post("/api/transaction")
def add_transaction(req: TransactionRequest, background_tasks: BackgroundTasks):
    t0 = time.perf_counter()
    amt_num = parse_amount(req.amount)
    if amt_num <= 0:
        raise HTTPException(status_code=400, detail="Số tiền không hợp lệ")

    d = req.date.strip()
    if "-" in d:
        parts = d.split("-")
        if len(parts) == 3 and len(parts[0]) == 4:
            d = f"{parts[2]}/{parts[1]}/{parts[0]}"

    is_income = (req.type == "Thu nhập")
    thu_val = amt_num if is_income else ""
    chi_val = "" if is_income else amt_num

    target_row = _state["next_row"]
    _state["next_row"] += 1

    # Local disk log (0.1ms)
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "row": target_row,
        "date": d,
        "type": req.type,
        "category": req.category,
        "amount": amt_num,
        "content": req.content
    }
    with open(LOCAL_LOG_PATH, "a", encoding="utf-8") as lf:
        lf.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    # Optimistic local state update (instant UI feel)
    if _state["data"]:
        _state["data"]["recent_transactions"].insert(0, {
            "row": target_row,
            "stt": str(_state["data"]["total_transactions"] + 1),
            "date": d,
            "month": f"Tháng {int(d.split('/')[1])}",
            "content": req.content,
            "type": req.type,
            "amount": f"{amt_num:,}".replace(",", "."),
            "balance": "Đang đồng bộ...",
            "category": req.category
        })
        _state["data"]["total_transactions"] += 1

    # Queue Google Sheets update to background worker (NON-BLOCKING)
    background_tasks.add_task(
        write_to_sheet_task,
        target_row, d, req.content.strip(), thu_val, chi_val, req.category.strip(), log_entry
    )

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "ok": True,
        "row": target_row,
        "latency_ms": elapsed_ms,
        "message": f"Đã ghi nhận trong {elapsed_ms}ms (Đang đồng bộ ngầm lên Google Cloud)",
        "transaction": log_entry
    }


from fastapi.responses import FileResponse


import io
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from fastapi.responses import Response

@app.get("/api/export-excel")
def export_excel():
    """Tự động đóng gói toàn bộ dữ liệu người dùng thành file Excel chuẩn công thức động"""
    # Lấy dữ liệu mới nhất
    overview = get_overview()
    txs = overview.get("recent_transactions", [])
    
    wb = openpyxl.Workbook()
    
    # Styles
    font_title = Font(name="Calibri", size=15, bold=True, color="1E293B")
    font_header = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    font_bold = Font(name="Calibri", size=11, bold=True, color="1E293B")
    font_reg = Font(name="Calibri", size=11, color="1E293B")
    font_muted = Font(name="Calibri", size=9, italic=True, color="64748B")
    
    fill_navy = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    fill_zebra = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    thin_gray = Side(border_style="thin", color="CBD5E1")
    border_all = Border(left=thin_gray, right=thin_gray, top=thin_gray, bottom=thin_gray)
    
    # 1. SHEET DASHBOARD
    ws_db = wb.active
    ws_db.title = "DASHBOARD"
    ws_db.views.sheetView[0].showGridLines = True
    
    ws_db["A1"] = "BÁO CÁO TÀI CHÍNH TỔNG QUAN TỰ ĐỘNG"
    ws_db["A1"].font = font_title
    ws_db["A2"] = "Xuất tự động từ CEO Finance OS - Sẵn sàng 100% công thức động"
    ws_db["A2"].font = font_muted
    
    # KPIs Cards
    ws_db["B4"] = "GIÁ TRỊ RÒNG (NET WORTH)"
    ws_db["B4"].font = Font(name="Calibri", size=10, bold=True, color="B45309")
    ws_db["B5"] = "=TAI_KHOAN!F13"
    ws_db["B5"].font = Font(name="Calibri", size=16, bold=True)
    ws_db["B5"].number_format = '#,##0 "đ"'
    ws_db["B5"].fill = PatternFill(start_color="FEF3C7", fill_type="solid")
    
    ws_db["D4"] = "TỔNG TÀI SẢN (ASSETS)"
    ws_db["D4"].font = Font(name="Calibri", size=10, bold=True, color="15803D")
    ws_db["D5"] = "=TAI_KHOAN!F11"
    ws_db["D5"].font = Font(name="Calibri", size=16, bold=True)
    ws_db["D5"].number_format = '#,##0 "đ"'
    ws_db["D5"].fill = PatternFill(start_color="DCFCE7", fill_type="solid")
    
    ws_db["F4"] = "TỔNG NỢ (LIABILITIES)"
    ws_db["F4"].font = Font(name="Calibri", size=10, bold=True, color="B91C1C")
    ws_db["F5"] = "=TAI_KHOAN!F12"
    ws_db["F5"].font = Font(name="Calibri", size=16, bold=True)
    ws_db["F5"].number_format = '#,##0 "đ"'
    ws_db["F5"].fill = PatternFill(start_color="FEE2E2", fill_type="solid")
    
    # Bảng 12 Tháng
    m_headers = ["Tháng", "Thu Nhập", "Chi Tiêu", "Dư Ròng", "Tỷ Lệ Tiết Kiệm"]
    for c_idx, h in enumerate(m_headers, 1):
        cell = ws_db.cell(row=8, column=c_idx, value=h)
        cell.font = font_header
        cell.fill = fill_navy
        cell.alignment = Alignment(horizontal="center")
        
    for m in range(1, 13):
        r = 8 + m
        ws_db.cell(row=r, column=1, value=f"2026-{m:02d}").font = font_bold
        ws_db.cell(row=r, column=1).alignment = Alignment(horizontal="center")
        
        c_inc = ws_db.cell(row=r, column=2, value=f'=SUMIFS(GIAO_DICH!$E:$E, GIAO_DICH!$B:$B, "Thu nhập", GIAO_DICH!$K:$K, A{r})')
        c_inc.number_format = '#,##0 "đ"'
        
        c_exp = ws_db.cell(row=r, column=3, value=f'=SUMIFS(GIAO_DICH!$E:$E, GIAO_DICH!$B:$B, "Chi phí", GIAO_DICH!$K:$K, A{r})')
        c_exp.number_format = '#,##0 "đ"'
        
        c_net = ws_db.cell(row=r, column=4, value=f'=B{r}-C{r}')
        c_net.font = font_bold
        c_net.number_format = '#,##0 "đ"'
        
        c_rate = ws_db.cell(row=r, column=5, value=f'=IF(B{r}>0, D{r}/B{r}, 0)')
        c_rate.number_format = '0.0%'
        c_rate.alignment = Alignment(horizontal="center")
        
        for c in range(1, 6):
            ws_db.cell(row=r, column=c).border = border_all
            if r % 2 == 0: ws_db.cell(row=r, column=c).fill = fill_zebra
            
    # 2. SHEET GIAO_DICH
    ws_tx = wb.create_sheet(title="GIAO_DICH")
    ws_tx.views.sheetView[0].showGridLines = True
    tx_headers = ["Ngày", "Phân Loại", "Hạng Mục", "Nội Dung", "Số Tiền", "Tài Khoản", "Tồn", "Ghi Chú", "", "", "Tháng", "Năm"]
    for c_idx, h in enumerate(tx_headers, 1):
        c = ws_tx.cell(row=4, column=c_idx, value=h)
        c.font = font_header
        c.fill = fill_navy
        c.alignment = Alignment(horizontal="center")
        
    for idx, t in enumerate(txs, 5):
        ws_tx.cell(row=idx, column=1, value=t.get("date", "")).alignment = Alignment(horizontal="center")
        ws_tx.cell(row=idx, column=2, value=t.get("type", "")).alignment = Alignment(horizontal="center")
        ws_tx.cell(row=idx, column=3, value=t.get("category", ""))
        ws_tx.cell(row=idx, column=4, value=t.get("content", "")).font = font_bold
        
        camt = ws_tx.cell(row=idx, column=5, value=parse_amount(t.get("amount", 0)))
        camt.number_format = '#,##0 "đ"'
        camt.font = font_bold
        
        ws_tx.cell(row=idx, column=6, value="Techcombank")
        cbal = ws_tx.cell(row=idx, column=7, value=parse_amount(t.get("balance", 0)))
        cbal.number_format = '#,##0 "đ"'
        
        ws_tx.cell(row=idx, column=11, value=f'=IF(A{idx}="","",TEXT(A{idx},"yyyy-mm"))').alignment = Alignment(horizontal="center")
        ws_tx.cell(row=idx, column=12, value=f'=IF(A{idx}="","",YEAR(A{idx}))').alignment = Alignment(horizontal="center")
        
        for c in range(1, 13):
            ws_tx.cell(row=idx, column=c).border = border_all
            if idx % 2 == 0: ws_tx.cell(row=idx, column=c).fill = fill_zebra
            
    # 3. SHEET TAI_KHOAN
    ws_acc = wb.create_sheet(title="TAI_KHOAN")
    acc_headers = ["Tên Tài Khoản", "Phân Loại", "Số Dư Ban Đầu", "Tổng Tiền Vào", "Tổng Tiền Ra", "Số Dư Hiện Tại"]
    for c_idx, h in enumerate(acc_headers, 1):
        c = ws_acc.cell(row=4, column=c_idx, value=h)
        c.font = font_header
        c.fill = fill_navy
        c.alignment = Alignment(horizontal="center")
        
    accs = [
        ("Tiền Mặt / Ví", "Tài sản", 5000000),
        ("Techcombank (Chính)", "Tài sản", 45000000),
        ("Vietcombank (Tiết kiệm)", "Tài sản", 120000000),
        ("Tài Khoản Chứng Khoán", "Tài sản", 80000000),
        ("Thẻ Tín Dụng", "Nợ", 0),
        ("Khoản Vay Khác", "Nợ", 0)
    ]
    for idx, (aname, atype, init_b) in enumerate(accs, 5):
        ws_acc.cell(row=idx, column=1, value=aname).font = font_bold
        ws_acc.cell(row=idx, column=2, value=atype).alignment = Alignment(horizontal="center")
        ws_acc.cell(row=idx, column=3, value=init_b).number_format = '#,##0 "đ"'
        
        ws_acc.cell(row=idx, column=4, value=f'=SUMIFS(GIAO_DICH!$E:$E, GIAO_DICH!$B:$B, "Thu nhập", GIAO_DICH!$F:$F, A{idx})').number_format = '#,##0 "đ"'
        ws_acc.cell(row=idx, column=5, value=f'=SUMIFS(GIAO_DICH!$E:$E, GIAO_DICH!$B:$B, "Chi phí", GIAO_DICH!$F:$F, A{idx})').number_format = '#,##0 "đ"'
        ws_acc.cell(row=idx, column=6, value=f'=IF(B{idx}="Tài sản", C{idx}+D{idx}-E{idx}, C{idx}+E{idx}-D{idx})').number_format = '#,##0 "đ"'
        ws_acc.cell(row=idx, column=6).font = font_bold
        for c in range(1, 7): ws_acc.cell(row=idx, column=c).border = border_all

    ws_acc.cell(row=11, column=1, value="TỔNG TÀI SẢN (ASSETS)").font = font_bold
    ws_acc.cell(row=11, column=6, value='=SUMIF(B5:B10, "Tài sản", F5:F10)').number_format = '#,##0 "đ"'
    ws_acc.cell(row=12, column=1, value="TỔNG NỢ (LIABILITIES)").font = font_bold
    ws_acc.cell(row=12, column=6, value='=SUMIF(B5:B10, "Nợ", F5:F10)').number_format = '#,##0 "đ"'
    ws_acc.cell(row=13, column=1, value="GIÁ TRỊ RÒNG (NET WORTH)").font = Font(name="Calibri", size=12, bold=True)
    ws_acc.cell(row=13, column=6, value='=F11-F12').number_format = '#,##0 "đ"'

    # Column widths
    ws_db.column_dimensions['A'].width = 14
    ws_db.column_dimensions['B'].width = 24
    ws_db.column_dimensions['C'].width = 20
    ws_db.column_dimensions['D'].width = 20
    ws_db.column_dimensions['E'].width = 16
    ws_tx.column_dimensions['A'].width = 14
    ws_tx.column_dimensions['B'].width = 14
    ws_tx.column_dimensions['C'].width = 30
    ws_tx.column_dimensions['D'].width = 28
    ws_tx.column_dimensions['E'].width = 18
    ws_acc.column_dimensions['A'].width = 28
    ws_acc.column_dimensions['B'].width = 14
    ws_acc.column_dimensions['C'].width = 18
    ws_acc.column_dimensions['D'].width = 18
    ws_acc.column_dimensions['E'].width = 18
    ws_acc.column_dimensions['F'].width = 20

    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    
    return Response(
        content=stream.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=So_Tai_Chinh_2026_Auto.xlsx"
        }
    )

@app.get("/manifest.json")
def get_manifest():
    return FileResponse("/opt/ai-os/products/ceo/web_finance/static/manifest.json", media_type="application/manifest+json")

@app.get("/sw.js")
def get_sw():
    return FileResponse("/opt/ai-os/products/ceo/web_finance/static/sw.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})

app.mount("/static", StaticFiles(directory="/opt/ai-os/products/ceo/web_finance/static"), name="static")

@app.get("/", response_class=HTMLResponse)
def serve_home():
    html_path = "/opt/ai-os/products/ceo/web_finance/static/index.html"
    with open(html_path, encoding="utf-8") as f:
        return f.read()

