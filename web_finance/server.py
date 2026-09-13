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

