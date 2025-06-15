from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict
import sqlite3
import asyncio
import random
import time

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB = "aviator.db"

# ==== DATABASE INITIALIZATION ====
def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            balance REAL DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            user_id TEXT,
            amount REAL,
            round_id INTEGER,
            cashed_out INTEGER DEFAULT 0,
            coefficient REAL DEFAULT 1.0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT, -- 'waiting', 'running', 'ended'
            start_time REAL,
            crash_point REAL
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ==== UTILS ====
def get_user_balance(user_id: str):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0.0

def set_user_balance(user_id: str, new_balance: float):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users(user_id, balance) VALUES (?, ?)", (user_id, new_balance))
    cur.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
    conn.commit()
    conn.close()

def add_balance(user_id: str, amount: float):
    bal = get_user_balance(user_id)
    set_user_balance(user_id, bal + amount)

def get_current_round():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT * FROM rounds WHERE status IN ('waiting', 'running') ORDER BY id DESC LIMIT 1")
    round = cur.fetchone()
    conn.close()
    return round

def create_new_round():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    crash_point = round(random.uniform(1.5, 5.0), 2)
    start_time = time.time() + 5  # wait 5s
    cur.execute("INSERT INTO rounds (status, start_time, crash_point) VALUES (?, ?, ?)", ("waiting", start_time, crash_point))
    conn.commit()
    conn.close()

def update_round_status(round_id: int, status: str):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("UPDATE rounds SET status = ? WHERE id = ?", (status, round_id))
    conn.commit()
    conn.close()

# ==== API ====
@app.get("/get_balance")
def get_balance(user_id: str):
    bal = get_user_balance(user_id)
    return {"balance": round(bal, 2)}

class BetRequest(BaseModel):
    user_id: str
    amount: float

@app.post("/place_bet")
def place_bet(bet: BetRequest):
    round = get_current_round()
    if not round:
        return {"error": "No active round"}
    round_id, status, start_time, crash_point = round

    if status != "waiting":
        return {"error": "Раунд басталып кетті"}

    user_balance = get_user_balance(bet.user_id)
    if bet.amount <= 0 or bet.amount > user_balance:
        return {"error": "Жарамсыз ставка"}

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO bets (user_id, amount, round_id) VALUES (?, ?, ?)",
                (bet.user_id, bet.amount, round_id))
    cur.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?",
                (bet.amount, bet.user_id))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post("/cashout")
def cashout(user_id: str):
    round = get_current_round()
    if not round:
        return {"error": "Ойын жоқ"}
    round_id, status, start_time, crash_point = round

    if status != "running":
        return {"error": "Кэшаутқа ерте"}

    now = time.time()
    flight_duration = now - start_time
    current_coef = round(1.0 + flight_duration * 0.1, 2)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT amount, cashed_out FROM bets WHERE user_id = ? AND round_id = ?", (user_id, round_id))
    row = cur.fetchone()

    if not row or row[1]:
        conn.close()
        return {"error": "Ставка жоқ немесе кэшаут жасалды"}

    amount = row[0]
    win = round(amount * current_coef, 2)
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (win, user_id))
    cur.execute("UPDATE bets SET cashed_out = 1, coefficient = ? WHERE user_id = ? AND round_id = ?",
                (current_coef, user_id, round_id))
    conn.commit()
    conn.close()

    return {"success": True, "win": win}

# ==== GAME LOOP ====
connected_clients: Dict[WebSocket, str] = {}

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients[ws] = ""

    try:
        while True:
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        del connected_clients[ws]

async def game_loop():
    while True:
        create_new_round()
        round = get_current_round()
        if not round:
            await asyncio.sleep(1)
            continue
        round_id, _, start_time, crash_point = round

        await broadcast({"status": "waiting", "countdown": int(start_time - time.time())})

        # WAIT BEFORE FLIGHT
        while time.time() < start_time:
            await broadcast({"status": "waiting", "countdown": int(start_time - time.time())})
            await asyncio.sleep(1)

        update_round_status(round_id, "running")
        start_time = time.time()
        coef = 1.0

        # FLIGHT
        while coef < crash_point:
            coef += 0.01
            await broadcast({"status": "running", "coefficient": round(coef, 2)})
            await asyncio.sleep(0.05)

        update_round_status(round_id, "ended")
        await broadcast({"status": "ended", "final_coef": round(coef, 2)})
        await asyncio.sleep(3)

async def broadcast(message: dict):
    to_remove = []
    for ws in connected_clients:
        try:
            await ws.send_json(message)
        except:
            to_remove.append(ws)
    for ws in to_remove:
        del connected_clients[ws]

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(game_loop())
