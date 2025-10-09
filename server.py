import os
import json
import signal
import shutil
import asyncio
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any

from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from dotenv import load_dotenv

from pyquotex.stable_api import Quotex
from assets import live_assets, otc_assets
from utils import get_payout_filtered_assets


ROOT = Path(__file__).parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
STRATEGY = ROOT / "strategy_breakout_loop.py"
STOP_FILE = ROOT / "STOP"


class StartSettings(BaseModel):
    payout: float = Field(84, ge=0, le=100)
    timeframe: int = Field(60, ge=15, le=3600)
    trade_percent: float = Field(2.0, ge=0.5, le=15.0)
    account: str = Field("PRACTICE")  # PRACTICE | REAL
    max_concurrent: int = Field(1, ge=1, le=10)
    run_minutes: int = Field(0, ge=0)  # 0 => indefinite
    payout_refresh_min: int = Field(10, ge=1, le=120)
    # The daily limit fields are now included in the settings
    daily_profit_limit: float = Field(0)
    daily_profit_is_percent: bool = Field(True)
    daily_loss_limit: float = Field(0)
    daily_loss_is_percent: bool = Field(True)

    @validator("account")
    def _acc(cls, v):
        v = v.upper()
        if v not in {"PRACTICE", "REAL"}:
            raise ValueError("account must be PRACTICE or REAL")
        return v


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


process: Optional[subprocess.Popen] = None
current_settings: Dict[str, Any] = {}


@app.get("/api/initial_data")
async def get_initial_data():
    load_dotenv()
    email = os.getenv("QX_EMAIL")
    password = os.getenv("QX_PASSWORD")

    if not email or not password:
        raise HTTPException(400, "Missing QX_EMAIL or QX_PASSWORD in .env")

    practice_balance = 0
    real_balance = 0
    tradable_assets = []

    try:
        print(f"🔗 Connecting to Quotex with email: {email}")
        client = Quotex(email=email, password=password, lang="en")
        connected, reason = await client.connect()
        
        if not connected:
            print(f"❌ Failed to connect to Quotex: {reason}")
            return {
                "balances": {"practice": 0, "real": 0},
                "assets": [],
                "email": email,
                "error": f"Connection failed: {reason}"
            }
        
        print("✅ Successfully connected to Quotex")

        # Fetch practice balance using change_account method
        print("💰 Fetching practice balance...")
        await client.change_account("PRACTICE")
        await asyncio.sleep(0.5)
        practice_balance = await client.get_balance()
        print(f"✅ Practice balance: ${practice_balance}")

        # Fetch real balance
        print("💰 Fetching real balance...")
        await client.change_account("REAL")
        await asyncio.sleep(0.5)
        real_balance = await client.get_balance()
        print(f"✅ Real balance: ${real_balance}")

        # Filter tradable assets
        print("🔍 Filtering tradable assets...")
        all_assets = live_assets + otc_assets
        tradable_assets = await get_payout_filtered_assets(client, all_assets, 84)
        
        await client.close()
        print("🔒 Connection closed")

    except Exception as e:
        print(f"❌ An error occurred: {e}")
        import traceback
        traceback.print_exc()
        return {
            "balances": {"practice": 0, "real": 0},
            "assets": [],
            "email": email,
            "error": str(e)
        }

    return {
        "balances": {
            "practice": practice_balance,
            "real": real_balance,
        },
        "assets": tradable_assets,
        "email": email,
    }


@app.get("/api/trade_logs")
async def get_trade_logs():
    active_trades = []
    trade_history = []
    daily_pnl = 0
    log_file = ROOT / "trades.log"
    if not log_file.exists():
        return {"active_trades": [], "trade_history": [], "daily_pnl": 0}

    try:
        with open(log_file, "r") as f:
            lines = f.readlines()

        today_str = datetime.utcnow().date().isoformat()

        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                log = json.loads(line)

                if log.get("timestamp", "").startswith(today_str):
                    if log.get("status") == "active":
                        log["live_pnl"] = "N/A"
                        active_trades.append(log)
                    else:
                        log["balance_after"] = "N/A"
                        trade_history.append(log)
                        # Add to daily P&L
                        pnl = log.get("pnl", 0)
                        if isinstance(pnl, (int, float)):
                            daily_pnl += pnl

            except json.JSONDecodeError:
                print(f"Skipping malformed log line: {line.strip()}")
                continue
    except Exception as e:
        print(f"Error reading or processing trades.log: {e}")

    return {"active_trades": active_trades, "trade_history": trade_history, "daily_pnl": daily_pnl}


def build_env(settings: StartSettings) -> Dict[str, str]:
    env = os.environ.copy()
    env.update({
        "QX_PAYOUT": str(settings.payout),
        "QX_TIMEFRAME": str(settings.timeframe),
        "QX_TRADE_PERCENT": str(settings.trade_percent),
        "QX_ACCOUNT": str(settings.account),
        "QX_RUN_MINUTES": str(settings.run_minutes),
        "QX_PAYOUT_REFRESH_MIN": str(settings.payout_refresh_min),
        "QX_DAILY_PROFIT": str(settings.daily_profit_limit),
        "QX_DAILY_PROFIT_IS_PERCENT": "1" if settings.daily_profit_is_percent else "0",
        "QX_DAILY_LOSS": str(settings.daily_loss_limit),
        "QX_DAILY_LOSS_IS_PERCENT": "1" if settings.daily_loss_is_percent else "0",
        "QX_MAX_CONCURRENT": str(settings.max_concurrent),
    })
    return env


@app.post("/api/start")
async def start_bot(settings: StartSettings):
    global process, current_settings
    if not (ROOT / ".env").exists():
        raise HTTPException(400, detail="Missing .env with QX_EMAIL and QX_PASSWORD")
    if process and process.poll() is None:
        raise HTTPException(400, detail="Bot already running")

    if STOP_FILE.exists():
        STOP_FILE.unlink()

    env = build_env(settings)
    py = str(VENV_PY if VENV_PY.exists() else shutil.which("python"))
    if not py:
        raise HTTPException(500, detail="Python interpreter not found")

    try:
        process = subprocess.Popen(
            [py, str(STRATEGY)], cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP') else 0,
        )
        current_settings = settings.dict()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(500, detail=f"Failed to start: {exc}")


@app.post("/api/stop")
async def stop_bot():
    global process
    STOP_FILE.write_text("stop")
    await asyncio.sleep(1.0)
    if process and process.poll() is None:
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            process.terminate()
        except Exception:
            pass
    return {"ok": True}


@app.get("/api/status")
def status():
    running = bool(process and process.poll() is None)
    return {
        "running": running,
        "settings": current_settings,
    }


@app.get("/api/refresh_assets")
async def refresh_assets(payout: float = 84):
    load_dotenv()
    email = os.getenv("QX_EMAIL")
    password = os.getenv("QX_PASSWORD")
    
    if not email or not password:
        raise HTTPException(400, "Missing credentials")
    
    try:
        print(f"🔄 Refreshing assets with payout threshold: {payout}%")
        client = Quotex(email=email, password=password, lang="en")
        connected, reason = await client.connect()
        
        if not connected:
            raise HTTPException(500, f"Connection failed: {reason}")
        
        all_assets = live_assets + otc_assets
        tradable_assets = await get_payout_filtered_assets(client, all_assets, payout)
        await client.close()
        
        return {"assets": tradable_assets}
        
    except Exception as e:
        print(f"❌ Error refreshing assets: {e}")
        raise HTTPException(500, str(e))


app.mount("/", StaticFiles(directory=str(ROOT / "frontend"), html=True), name="static")
