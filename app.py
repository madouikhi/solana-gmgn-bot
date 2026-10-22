"""
Solana GMGN Paper Trading Server (FastAPI) - v2
Serves the dashboard and REST API for the 1 SOL -> 10 SOL paper challenge.
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine import engine

HERE = os.path.dirname(__file__)
STATIC_DIR = os.path.join(HERE, "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(engine.start_worker())
    yield
    await engine.stop_worker()
    task.cancel()


app = FastAPI(title="Solana GMGN Paper Trading Bot", lifespan=lifespan)


class TokenAddressPayload(BaseModel):
    token_address: str


@app.get("/api/status")
async def get_status():
    p, s = engine.portfolio, engine.settings
    equity = engine.equity_sol()
    start = p.get("initial_balance_sol", s["initial_balance_sol"])
    goal = s["goal_sol"]
    total = p["total_trades"]
    progress = (equity - start) / (goal - start) * 100.0 if goal > start else 0.0
    return {
        "virtual_balance_sol": round(p["virtual_balance_sol"], 4),
        "equity_sol": round(equity, 4),
        "equity_usd": round(equity * engine.sol_price_usd, 2),
        "initial_sol": start,
        "goal_sol": goal,
        "goal_progress_pct": round(max(0.0, min(100.0, progress)), 1),
        "multiple": round(equity / start, 2) if start else 0,
        "realized_pnl_sol": round(p["realized_pnl_sol"], 4),
        "fees_paid_sol": round(p.get("fees_paid_sol", 0.0), 4),
        "total_trades": total,
        "winning_trades": p["winning_trades"],
        "losing_trades": p["losing_trades"],
        "win_rate_pct": round(p["winning_trades"] / total * 100.0, 1) if total else 0.0,
        "sol_price_usd": round(engine.sol_price_usd, 2),
        "auto_trading": s.get("auto_trading", True),
        "challenge_status": p.get("status", "running"),
        "active_positions_count": len(p["active_positions"]),
        "max_positions": s["max_open_positions"],
        "next_trade_size_sol": round(engine._trade_size(), 4),
    }


@app.get("/api/positions")
async def get_positions():
    out = []
    for pos in engine.portfolio["active_positions"].values():
        out.append({**pos, "value_sol": round(engine.position_value_sol(pos), 4)})
    return out


@app.get("/api/trades")
async def get_trades():
    return engine.portfolio["trade_history"]


@app.get("/api/candidates")
async def get_candidates():
    return engine.scanned_candidates


@app.get("/api/settings")
async def get_settings():
    return engine.settings


@app.post("/api/toggle-autotrade")
async def toggle_autotrade():
    engine.settings["auto_trading"] = not engine.settings.get("auto_trading", True)
    engine.save_settings()
    return {"auto_trading": engine.settings["auto_trading"]}


@app.post("/api/close-position")
async def close_position(payload: TokenAddressPayload):
    ok = await engine.execute_paper_sell(payload.token_address, "إغلاق يدوي")
    if not ok:
        raise HTTPException(status_code=404, detail="Position not found")
    return {"status": "success"}


@app.post("/api/buy-manual")
async def buy_manual(payload: TokenAddressPayload):
    addr = payload.token_address.strip()
    pairs = await engine._best_pairs([addr])
    if addr not in pairs:
        raise HTTPException(status_code=404, detail="لم يتم العثور على مسبح سيولة لهذه العملة على سولانا")
    cand = engine._evaluate(pairs[addr])
    if not await engine.execute_paper_buy(cand, manual=True):
        raise HTTPException(status_code=400, detail="تعذر الشراء: الرصيد غير كافٍ أو الحد الأقصى للصفقات")
    return {"status": "success", "symbol": cand["symbol"]}


@app.post("/api/reset")
async def reset_portfolio():
    engine.reset_portfolio()
    return {"status": "success"}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000)
