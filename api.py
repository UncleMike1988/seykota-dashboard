"""
THE SEYKOTA MACHINE — Dashboard API
FastAPI backend that proxies data from the Vultr data API.
Runs on Railway, serves the frontend, forwards all data requests to Vultr.
"""

import os
import httpx
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
VULTR_DATA_URL = os.getenv("VULTR_DATA_URL", "http://149.28.252.29:8001")


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


async def fetch_vultr(endpoint: str, params: dict = None):
    """Call the Vultr data API with authentication."""
    headers = {"X-Dashboard-Token": DASHBOARD_TOKEN} if DASHBOARD_TOKEN else {}
    url = f"{VULTR_DATA_URL}{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code == 401:
                raise HTTPException(status_code=502, detail="Vultr auth failed")
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Cannot reach Vultr data API")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Vultr data API timeout")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Vultr returned {e.response.status_code}")


@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{VULTR_DATA_URL}/health")
            vultr_ok = resp.status_code == 200
    except Exception:
        vultr_ok = False
    return {"status": "ok", "vultr_connected": vultr_ok}


@app.get("/api/auth")
async def auth_check(token: str = ""):
    if not DASHBOARD_TOKEN:
        return {"authenticated": True}
    if token == DASHBOARD_TOKEN:
        return {"authenticated": True}
    raise HTTPException(status_code=401, detail="Invalid token")


@app.get("/api/status", dependencies=[Depends(verify_token)])
async def get_status(mode: str = Query("paper")):
    data = await fetch_vultr("/data/status")
    data["mode"] = mode
    return data


@app.get("/api/positions", dependencies=[Depends(verify_token)])
async def get_positions(mode: str = Query("paper")):
    return await fetch_vultr("/data/positions")


@app.get("/api/trades", dependencies=[Depends(verify_token)])
async def get_trades(mode: str = Query("paper"), limit: int = Query(20)):
    return await fetch_vultr("/data/trades", params={"limit": limit})


@app.get("/api/equity-curve", dependencies=[Depends(verify_token)])
async def get_equity_curve(mode: str = Query("paper"), days: int = Query(365)):
    return await fetch_vultr("/data/equity-curve", params={"days": days})


@app.get("/api/veto-log", dependencies=[Depends(verify_token)])
async def get_veto_log(mode: str = Query("paper"), limit: int = Query(10)):
    return await fetch_vultr("/data/veto-log", params={"limit": limit})


@app.get("/api/instruments", dependencies=[Depends(verify_token)])
async def get_instruments(mode: str = Query("paper")):
    return await fetch_vultr("/data/instruments")


@app.get("/api/monthly-pnl", dependencies=[Depends(verify_token)])
async def get_monthly_pnl(mode: str = Query("paper")):
    return await fetch_vultr("/data/monthly-pnl")


@app.get("/api/risk-exposure", dependencies=[Depends(verify_token)])
async def get_risk_exposure(mode: str = Query("paper")):
    return await fetch_vultr("/data/risk-exposure")


# Serve frontend
FRONTEND_DIR = Path(__file__).parent / "frontend"


@app.get("/")
async def serve_index():
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
