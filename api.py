"""
THE SEYKOTA MACHINE — Dashboard API
FastAPI backend serving trading data from SQLite.
"""

import os
import aiosqlite
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.requests import Request

app = FastAPI(title="Seykota Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")
PAPER_DB = os.getenv("PAPER_DB_PATH", "/app/data/seykota.db")
LIVE_DB = os.getenv("LIVE_DB_PATH", "/app/data/live_seykota.db")

INSTRUMENTS = [
    {"symbol": "MES", "exchange": "CME", "multiplier": 5.0, "name": "Micro S&P 500"},
    {"symbol": "MNQ", "exchange": "CME", "multiplier": 2.0, "name": "Micro Nasdaq 100"},
    {"symbol": "M2K", "exchange": "CME", "multiplier": 5.0, "name": "Micro Russell 2000"},
    {"symbol": "MGC", "exchange": "COMEX", "multiplier": 10.0, "name": "Micro Gold"},
    {"symbol": "MCL", "exchange": "NYMEX", "multiplier": 100.0, "name": "Micro Crude Oil"},
    {"symbol": "M6E", "exchange": "CME", "multiplier": 12500.0, "name": "Micro Euro FX"},
]


def get_db_path(mode: str = "paper") -> str:
    if mode == "live":
        return LIVE_DB
    return PAPER_DB


async def verify_token(request: Request):
    if not DASHBOARD_TOKEN:
        return
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    else:
        token = request.query_params.get("token", "")
    if token != DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


async def query_db(db_path: str, sql: str, params: tuple = ()):
    if not Path(db_path).exists():
        return []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def query_one(db_path: str, sql: str, params: tuple = ()):
    rows = await query_db(db_path, sql, params)
    return rows[0] if rows else None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/auth")
async def auth_check(token: str = ""):
    if not DASHBOARD_TOKEN:
        return {"authenticated": True}
    if token == DASHBOARD_TOKEN:
        return {"authenticated": True}
    raise HTTPException(status_code=401, detail="Invalid token")


@app.get("/api/status", dependencies=[Depends(verify_token)])
async def get_status(mode: str = Query("paper")):
    db = get_db_path(mode)
    if not Path(db).exists():
        return {"exists": False, "mode": mode}

    latest = await query_one(db, "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT 1")
    day_count = await query_one(db, "SELECT COUNT(*) as cnt FROM daily_pnl")
    peak = await query_one(db, "SELECT COALESCE(MAX(equity), 100000) as peak FROM daily_pnl")
    open_risk = await query_one(
        db, "SELECT COALESCE(SUM(risk_amount), 0) as total FROM positions WHERE status = 'open'"
    )

    equity = latest["equity"] if latest else 100000
    daily_pnl = latest["daily_pnl"] if latest else 0
    drawdown = latest["drawdown_pct"] if latest else 0
    total_risk = open_risk["total"] if open_risk else 0
    days = day_count["cnt"] if day_count else 0
    peak_eq = peak["peak"] if peak else 100000

    return {
        "exists": True,
        "mode": mode,
        "equity": equity,
        "daily_pnl": daily_pnl,
        "drawdown_pct": drawdown,
        "total_risk": total_risk,
        "total_risk_pct": (total_risk / equity * 100) if equity else 0,
        "peak_equity": peak_eq,
        "paper_days": days,
        "date": latest["date"] if latest else None,
    }


@app.get("/api/positions", dependencies=[Depends(verify_token)])
async def get_positions(mode: str = Query("paper")):
    db = get_db_path(mode)
    if not Path(db).exists():
        return []
    rows = await query_db(
        db,
        """SELECT id, symbol, direction, entry_date, entry_price, contracts,
                  stop_price, trail_stop, pyramid_unit, risk_amount, status
           FROM positions WHERE status = 'open' ORDER BY entry_date DESC"""
    )
    return rows


@app.get("/api/trades", dependencies=[Depends(verify_token)])
async def get_trades(mode: str = Query("paper"), limit: int = Query(20)):
    db = get_db_path(mode)
    if not Path(db).exists():
        return []
    rows = await query_db(
        db,
        """SELECT id, timestamp, symbol, action, direction, contracts, price,
                  position_id, reason, pnl
           FROM trades ORDER BY timestamp DESC LIMIT ?""",
        (limit,)
    )
    return rows


@app.get("/api/equity-curve", dependencies=[Depends(verify_token)])
async def get_equity_curve(mode: str = Query("paper"), days: int = Query(365)):
    db = get_db_path(mode)
    if not Path(db).exists():
        return []
    rows = await query_db(
        db,
        "SELECT date, equity, daily_pnl, drawdown_pct FROM daily_pnl ORDER BY date ASC"
    )
    if days and len(rows) > days:
        rows = rows[-days:]
    return rows


@app.get("/api/veto-log", dependencies=[Depends(verify_token)])
async def get_veto_log(mode: str = Query("paper"), limit: int = Query(10)):
    db = get_db_path(mode)
    if not Path(db).exists():
        return []
    rows = await query_db(
        db,
        """SELECT id, timestamp, symbol, signal_type, direction, price,
                  ai_veto, ai_reason, acted_on
           FROM signals WHERE ai_veto IS NOT NULL
           ORDER BY timestamp DESC LIMIT ?""",
        (limit,)
    )
    return rows


@app.get("/api/instruments", dependencies=[Depends(verify_token)])
async def get_instruments(mode: str = Query("paper")):
    db = get_db_path(mode)
    results = []

    for inst in INSTRUMENTS:
        sym = inst["symbol"]
        info = {**inst, "trend": "FLAT", "ema_fast": None, "ema_slow": None,
                "last_price": None, "position": None, "pnl": 0, "sparkline": []}

        if Path(db).exists():
            # Get last price
            bar = await query_one(
                db, "SELECT close FROM daily_bars WHERE symbol = ? ORDER BY date DESC LIMIT 1",
                (sym,)
            )
            if bar:
                info["last_price"] = bar["close"]

            # Get latest signal for EMA data
            sig = await query_one(
                db,
                """SELECT ema_fast, ema_slow FROM signals
                   WHERE symbol = ? AND ema_fast IS NOT NULL
                   ORDER BY timestamp DESC LIMIT 1""",
                (sym,)
            )
            if sig and sig["ema_fast"] and sig["ema_slow"]:
                info["ema_fast"] = sig["ema_fast"]
                info["ema_slow"] = sig["ema_slow"]
                diff = (sig["ema_fast"] - sig["ema_slow"]) / sig["ema_slow"]
                if diff > 0.005:
                    info["trend"] = "LONG"
                elif diff < -0.005:
                    info["trend"] = "SHORT"

            # Get open position
            pos = await query_one(
                db,
                "SELECT direction, contracts, entry_price FROM positions WHERE symbol = ? AND status = 'open' LIMIT 1",
                (sym,)
            )
            if pos:
                info["position"] = pos["direction"].upper()
                if bar:
                    mult = inst["multiplier"]
                    if pos["direction"] == "long":
                        info["pnl"] = (bar["close"] - pos["entry_price"]) * mult * pos["contracts"]
                    else:
                        info["pnl"] = (pos["entry_price"] - bar["close"]) * mult * pos["contracts"]

            # Sparkline: last 20 closes
            bars = await query_db(
                db,
                "SELECT close FROM daily_bars WHERE symbol = ? ORDER BY date DESC LIMIT 20",
                (sym,)
            )
            info["sparkline"] = [b["close"] for b in reversed(bars)]

        results.append(info)

    return results


@app.get("/api/monthly-pnl", dependencies=[Depends(verify_token)])
async def get_monthly_pnl(mode: str = Query("paper")):
    db = get_db_path(mode)
    if not Path(db).exists():
        return []
    rows = await query_db(
        db,
        """SELECT date, daily_pnl FROM daily_pnl ORDER BY date ASC"""
    )
    # Aggregate by month
    months = {}
    for row in rows:
        month_key = row["date"][:7]  # YYYY-MM
        if month_key not in months:
            months[month_key] = 0
        months[month_key] += row["daily_pnl"]
    return [{"month": k, "pnl": v} for k, v in sorted(months.items())]


@app.get("/api/risk-exposure", dependencies=[Depends(verify_token)])
async def get_risk_exposure(mode: str = Query("paper")):
    db = get_db_path(mode)
    if not Path(db).exists():
        return {"instruments": [], "total_risk": 0, "max_risk": 12.0}

    latest = await query_one(db, "SELECT equity FROM daily_pnl ORDER BY date DESC LIMIT 1")
    equity = latest["equity"] if latest else 100000

    instrument_risk = []
    for inst in INSTRUMENTS:
        risk_row = await query_one(
            db,
            "SELECT COALESCE(SUM(risk_amount), 0) as risk FROM positions WHERE symbol = ? AND status = 'open'",
            (inst["symbol"],)
        )
        risk = risk_row["risk"] if risk_row else 0
        instrument_risk.append({
            "symbol": inst["symbol"],
            "risk": risk,
            "risk_pct": (risk / equity * 100) if equity else 0
        })

    total = sum(r["risk"] for r in instrument_risk)
    return {
        "instruments": instrument_risk,
        "total_risk": total,
        "total_risk_pct": (total / equity * 100) if equity else 0,
        "max_risk": 12.0,
        "equity": equity,
    }


# Serve frontend
FRONTEND_DIR = Path(__file__).parent / "frontend"


@app.get("/")
async def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
