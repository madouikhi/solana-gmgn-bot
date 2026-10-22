"""
Solana GMGN Paper Trading Engine (v2 - "1 SOL -> 10 SOL" challenge)

Lessons applied from the first 19 paper trades:
  1. Stops slipped (-15% planned, -21% avg filled) -> fast 2s position monitor + tighter stop.
  2. Same coin re-bought 7 times while topping -> max entries per token + long cooldown after a loss.
  3. Low-liquidity rugs (-92%..-96%) -> higher liquidity floor, minimum token age, no buying into dumps.
  4. Paper fills were too optimistic -> simulated slippage + per-transaction fee.
  5. Wins were small relative to losses -> partial take-profit, breakeven stop, trailing the rest.
  6. Compounding toward a goal -> position size = % of current equity, auto-stop at goal or floor.
All money here is virtual. Prices come live from DexScreener; links point to GMGN.ai.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("SolanaGMGNBot")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
SOL_MINT = "So11111111111111111111111111111111111111112"
DEX = "https://api.dexscreener.com"

DEFAULT_SETTINGS = {
    # Challenge
    "initial_balance_sol": 1.0,
    "goal_sol": 10.0,
    "floor_sol": 0.3,               # stop trading if equity falls below this
    # Sizing (compounding)
    "position_size_pct": 20.0,      # % of current equity per trade
    "min_trade_sol": 0.05,
    "max_open_positions": 3,
    # Realism
    "slippage_pct": 1.5,            # applied on every buy and sell
    "fee_per_tx_sol": 0.001,        # priority fee + gas per transaction
    # Exits
    "stop_loss_pct": 12.0,
    "tp1_pct": 30.0,                # first target: sell part, move stop to breakeven
    "tp1_sell_fraction": 0.5,
    "trailing_distance_pct": 15.0,  # trail the rest from its peak after TP1
    "time_stop_min": 10,            # exit dead trades that go nowhere
    "time_stop_band_pct": 5.0,
    # Entry filters
    "min_liquidity_usd": 15000.0,
    "min_volume_5m_usd": 8000.0,
    "min_buy_ratio": 0.55,
    "min_age_min": 5,
    "max_age_min": 360,
    "max_m5_pump_pct": 60.0,        # don't buy the top of a vertical candle
    "min_h1_change_pct": -15.0,     # don't buy coins already dumping
    "min_score": 50,
    # Re-entry discipline
    "max_entries_per_token": 2,
    "loss_cooldown_min": 30,
    "win_cooldown_min": 5,
    # Loops
    "scan_interval_sec": 8,
    "monitor_interval_sec": 2,
    "auto_trading": True,
    "ref_code": "v9ZPMY3n",
}


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def new_portfolio(initial: float) -> Dict:
    return {
        "virtual_balance_sol": initial,
        "initial_balance_sol": initial,
        "realized_pnl_sol": 0.0,
        "realized_pnl_usd": 0.0,
        "fees_paid_sol": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "peak_equity_sol": initial,
        "status": "running",        # running | goal_reached | floor_hit
        "active_positions": {},
        "trade_history": [],
        "token_stats": {},          # addr -> {entries, last_exit, last_pnl}
        "started_at": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
    }


class PaperTradingEngine:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.settings = self._load_settings()
        self.portfolio = self._load_portfolio()
        self.sol_price_usd = 100.0
        self.scanned_candidates: List[Dict] = []
        self.is_running = False
        self._lock = asyncio.Lock()
        self.client: Optional[httpx.AsyncClient] = None

    # ------------------------------------------------------------------ persistence
    def _load_settings(self) -> Dict:
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                # Only keep keys that still exist in v2
                return {**DEFAULT_SETTINGS, **{k: v for k, v in saved.items() if k in DEFAULT_SETTINGS}}
            except Exception as e:
                logger.error(f"Error loading settings: {e}")
        return DEFAULT_SETTINGS.copy()

    def save_settings(self):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=2, ensure_ascii=False)

    def _load_portfolio(self) -> Dict:
        if os.path.exists(PORTFOLIO_FILE):
            try:
                with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                    p = json.load(f)
                if "token_stats" in p and "initial_balance_sol" in p:   # v2 format
                    return p
            except Exception as e:
                logger.error(f"Error loading portfolio: {e}")
        return new_portfolio(self.settings["initial_balance_sol"])

    def save_portfolio(self):
        self.portfolio["last_updated"] = datetime.now().isoformat()
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(self.portfolio, f, indent=2, ensure_ascii=False)

    def reset_portfolio(self):
        self.portfolio = new_portfolio(self.settings["initial_balance_sol"])
        self.settings["auto_trading"] = True
        self.save_settings()
        self.save_portfolio()
        logger.info(f"Portfolio reset to {self.settings['initial_balance_sol']} SOL.")

    def build_gmgn_url(self, token_address: str) -> str:
        return f"https://gmgn.ai/sol/token/{token_address}?ref={self.settings.get('ref_code', 'v9ZPMY3n')}"

    # ------------------------------------------------------------------ valuation
    def position_value_sol(self, pos: Dict, price: Optional[float] = None) -> float:
        """Liquidation value of remaining tokens (after slippage, before fee)."""
        price = price if price is not None else pos["current_price_usd"]
        slip = self.settings["slippage_pct"] / 100.0
        return pos["tokens_remaining"] * price * (1 - slip) / pos["sol_entry_price"]

    def position_roi_pct(self, pos: Dict) -> float:
        total = pos["realized_sol"] + self.position_value_sol(pos)
        return (total - pos["cost_sol"]) / pos["cost_sol"] * 100.0 if pos["cost_sol"] > 0 else 0.0

    def equity_sol(self) -> float:
        p = self.portfolio
        return p["virtual_balance_sol"] + sum(self.position_value_sol(x) for x in p["active_positions"].values())

    # ------------------------------------------------------------------ market data
    async def update_sol_price(self):
        try:
            r = await self.client.get(f"{DEX}/latest/dex/tokens/{SOL_MINT}", timeout=6.0)
            if r.status_code == 200:
                pairs = r.json().get("pairs") or []
                if pairs:
                    p = _f(pairs[0].get("priceUsd"))
                    if p > 0:
                        self.sol_price_usd = p
        except Exception as e:
            logger.debug(f"SOL price error: {e}")

    async def _best_pairs(self, addresses: List[str]) -> Dict[str, Dict]:
        """Return the highest-liquidity Solana pair per token (ignores bonding-curve pairs with no liquidity)."""
        best: Dict[str, Dict] = {}
        for i in range(0, len(addresses), 30):
            chunk = addresses[i:i + 30]
            try:
                r = await self.client.get(f"{DEX}/latest/dex/tokens/{','.join(chunk)}", timeout=8.0)
                if r.status_code != 200:
                    continue
                for p in r.json().get("pairs") or []:
                    if p.get("chainId") != "solana":
                        continue
                    addr = (p.get("baseToken") or {}).get("address")
                    if not addr or addr == SOL_MINT or _f(p.get("priceUsd")) <= 0:
                        continue
                    liq = _f((p.get("liquidity") or {}).get("usd"))
                    if addr not in best or liq > best[addr]["_liq"]:
                        p["_liq"] = liq
                        best[addr] = p
            except Exception as e:
                logger.debug(f"Pair fetch error: {e}")
        return best

    async def _discover_addresses(self) -> List[str]:
        found = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
            try:
                r = await self.client.get(DEX + path, timeout=7.0)
                if r.status_code == 200:
                    for item in r.json():
                        if item.get("chainId") == "solana" and item.get("tokenAddress"):
                            found.append(item["tokenAddress"])
            except Exception as e:
                logger.debug(f"Discovery error {path}: {e}")
        # de-duplicate, keep order
        return list(dict.fromkeys(found))[:60]

    def _evaluate(self, p: Dict) -> Dict:
        s = self.settings
        bt = p.get("baseToken") or {}
        addr = bt.get("address")
        price = _f(p.get("priceUsd"))
        liq = p["_liq"]
        vol5 = _f((p.get("volume") or {}).get("m5"))
        tx5 = (p.get("txns") or {}).get("m5") or {}
        buys, sells = int(_f(tx5.get("buys"))), int(_f(tx5.get("sells")))
        buy_ratio = buys / max(1, buys + sells)
        chg = p.get("priceChange") or {}
        m5, h1 = _f(chg.get("m5")), _f(chg.get("h1"))
        created = p.get("pairCreatedAt")
        age_min = (time.time() * 1000 - created) / 60000 if created else 0

        reasons = []
        if liq < s["min_liquidity_usd"]:
            reasons.append(f"سيولة ضعيفة ${liq:,.0f}")
        if vol5 < s["min_volume_5m_usd"]:
            reasons.append(f"حجم 5د ضعيف ${vol5:,.0f}")
        if buy_ratio < s["min_buy_ratio"]:
            reasons.append(f"ضغط بيع ({buy_ratio*100:.0f}% شراء)")
        if age_min < s["min_age_min"]:
            reasons.append(f"جديدة جداً ({age_min:.0f}د)")
        if age_min > s["max_age_min"]:
            reasons.append(f"قديمة ({age_min/60:.1f}س)")
        if m5 <= 0:
            reasons.append("لا يوجد زخم 5د")
        if m5 > s["max_m5_pump_pct"]:
            reasons.append(f"شمعة عمودية +{m5:.0f}%")
        if h1 < s["min_h1_change_pct"]:
            reasons.append(f"هابطة 1س {h1:.0f}%")

        stats = self.portfolio["token_stats"].get(addr, {})
        if stats.get("entries", 0) >= s["max_entries_per_token"]:
            reasons.append("تم تداولها بما يكفي")
        if stats.get("last_exit"):
            cd = s["loss_cooldown_min"] if stats.get("last_pnl", 0) < 0 else s["win_cooldown_min"]
            left = cd * 60 - (time.time() - stats["last_exit"])
            if left > 0:
                reasons.append(f"تبريد {left/60:.0f}د")

        score = 0
        if not reasons:
            score = 30
            score += min(25, int(m5 * 1.2))                                  # momentum
            score += min(20, int(max(0.0, buy_ratio - 0.5) * 100))          # buy pressure
            score += min(15, int(vol5 / max(1.0, liq) * 20))                 # turnover
            score += 10 if liq >= 30000 else 0                               # deeper pool
        return {
            "token_address": addr,
            "pair_address": p.get("pairAddress", ""),
            "symbol": bt.get("symbol", "?"),
            "name": bt.get("name", ""),
            "dex": p.get("dexId", ""),
            "price_usd": price,
            "liquidity_usd": liq,
            "volume_5m": vol5,
            "buy_ratio": round(buy_ratio, 2),
            "price_change_5m": m5,
            "price_change_1h": h1,
            "age_minutes": round(age_min, 1),
            "passed": not reasons and score >= s["min_score"],
            "rejection_reasons": reasons or ([] if score >= s["min_score"] else [f"نقاط منخفضة {score}"]),
            "score": min(100, score),
            "dex_url": p.get("url", ""),
            "gmgn_url": self.build_gmgn_url(addr),
        }

    async def fetch_solana_candidates(self) -> List[Dict]:
        addrs = await self._discover_addresses()
        if not addrs:
            return []
        pairs = await self._best_pairs(addrs)
        cands = [self._evaluate(p) for p in pairs.values()]
        cands.sort(key=lambda c: (c["passed"], c["score"]), reverse=True)
        return cands

    # ------------------------------------------------------------------ trading
    def _trade_size(self) -> float:
        s = self.settings
        return max(s["min_trade_sol"], self.equity_sol() * s["position_size_pct"] / 100.0)

    async def execute_paper_buy(self, c: Dict, manual: bool = False) -> bool:
        async with self._lock:
            s, p = self.settings, self.portfolio
            addr = c["token_address"]
            if addr in p["active_positions"] or c["price_usd"] <= 0:
                return False
            if len(p["active_positions"]) >= s["max_open_positions"]:
                return False
            if p["status"] != "running" and not manual:
                return False

            size = round(self._trade_size(), 4)
            fee = s["fee_per_tx_sol"]
            if p["virtual_balance_sol"] < size + fee:
                size = round(p["virtual_balance_sol"] - fee, 4)
                if size < s["min_trade_sol"]:
                    return False

            slip = s["slippage_pct"] / 100.0
            fill_price = c["price_usd"] * (1 + slip)
            tokens = size * self.sol_price_usd / fill_price

            p["virtual_balance_sol"] -= size + fee
            p["fees_paid_sol"] += fee
            st = p["token_stats"].setdefault(addr, {"entries": 0})
            st["entries"] = st.get("entries", 0) + 1

            p["active_positions"][addr] = {
                "id": str(uuid.uuid4())[:8],
                "token_address": addr,
                "symbol": c["symbol"],
                "name": c["name"],
                "entry_price_usd": c["price_usd"],
                "fill_price_usd": fill_price,
                "current_price_usd": c["price_usd"],
                "peak_price_usd": c["price_usd"],
                "sol_entry_price": self.sol_price_usd,
                "amount_sol": size,
                "cost_sol": size + fee,
                "tokens_remaining": tokens,
                "realized_sol": 0.0,
                "tp1_done": False,
                "stop_price_usd": fill_price * (1 - s["stop_loss_pct"] / 100.0),
                "current_roi_pct": round(-((size + fee) - size * (1 - slip) / (1 + slip)) / (size + fee) * 100, 2),
                "highest_roi_pct": 0.0,
                "entry_time": datetime.now().isoformat(),
                "entry_timestamp": time.time(),
                "entry_features": {k: c.get(k) for k in ("liquidity_usd", "volume_5m", "buy_ratio",
                                                         "price_change_5m", "price_change_1h",
                                                         "age_minutes", "score", "dex")},
                "manual": manual,
                "gmgn_url": c["gmgn_url"],
                "dex_url": c.get("dex_url", ""),
            }
            self.save_portfolio()
            logger.info(f"[BUY] {c['symbol']} {size} SOL @ ${c['price_usd']:.8g} "
                        f"(score {c.get('score')}, liq ${c.get('liquidity_usd', 0):,.0f})")
            return True

    def _sell_fraction_locked(self, addr: str, fraction: float, price: float, reason: str) -> Optional[float]:
        """Sell a fraction of a position. Caller must hold the lock. Returns SOL received."""
        p, s = self.portfolio, self.settings
        pos = p["active_positions"].get(addr)
        if not pos or price <= 0:
            return None
        fraction = min(1.0, max(0.0, fraction))
        tokens = pos["tokens_remaining"] * fraction
        fee = s["fee_per_tx_sol"]
        gross = tokens * price * (1 - s["slippage_pct"] / 100.0) / pos["sol_entry_price"]
        net = max(0.0, gross - fee)

        pos["tokens_remaining"] -= tokens
        pos["realized_sol"] += net
        pos["cost_sol"] += 0  # fees on exit are reflected via net
        p["virtual_balance_sol"] += net
        p["fees_paid_sol"] += fee

        if fraction < 1.0 and pos["tokens_remaining"] > 0:
            logger.info(f"[PARTIAL] {pos['symbol']} sold {fraction*100:.0f}% -> +{net:.4f} SOL ({reason})")
            return net

        # Fully closed
        p["active_positions"].pop(addr, None)
        pnl = pos["realized_sol"] - pos["cost_sol"]
        pnl_pct = pnl / pos["cost_sol"] * 100.0
        p["realized_pnl_sol"] += pnl
        p["realized_pnl_usd"] += pnl * self.sol_price_usd
        p["total_trades"] += 1
        p["winning_trades" if pnl > 0 else "losing_trades"] += 1
        st = p["token_stats"].setdefault(addr, {"entries": 1})
        st["last_exit"] = time.time()
        st["last_pnl"] = pnl

        p["trade_history"].insert(0, {
            "id": pos["id"],
            "token_address": addr,
            "symbol": pos["symbol"],
            "name": pos["name"],
            "amount_sol": pos["amount_sol"],
            "entry_price_usd": pos["entry_price_usd"],
            "exit_price_usd": price,
            "peak_roi_pct": pos["highest_roi_pct"],
            "pnl_sol": round(pnl, 4),
            "pnl_usd": round(pnl * self.sol_price_usd, 2),
            "pnl_pct": round(pnl_pct, 2),
            "tp1_hit": pos["tp1_done"],
            "entry_time": pos["entry_time"],
            "exit_time": datetime.now().isoformat(),
            "hold_duration_sec": int(time.time() - pos["entry_timestamp"]),
            "exit_reason": reason,
            "entry_features": pos.get("entry_features", {}),
            "gmgn_url": pos["gmgn_url"],
        })
        p["trade_history"] = p["trade_history"][:200]
        logger.info(f"[CLOSE] {pos['symbol']} {reason} | PnL {pnl:+.4f} SOL ({pnl_pct:+.1f}%)")
        return net

    async def execute_paper_sell(self, addr: str, reason: str, price: Optional[float] = None) -> bool:
        async with self._lock:
            pos = self.portfolio["active_positions"].get(addr)
            if not pos:
                return False
            res = self._sell_fraction_locked(addr, 1.0, price or pos["current_price_usd"], reason)
            self._check_challenge_locked()
            self.save_portfolio()
            return res is not None

    def _check_challenge_locked(self):
        p, s = self.portfolio, self.settings
        if p["status"] != "running":
            return
        eq = self.equity_sol()
        p["peak_equity_sol"] = max(p.get("peak_equity_sol", eq), eq)
        if eq >= s["goal_sol"]:
            p["status"] = "goal_reached"
            s["auto_trading"] = False
            self.save_settings()
            logger.info(f"🏆 GOAL REACHED: equity {eq:.4f} SOL >= {s['goal_sol']} SOL. Auto-trading stopped.")
        elif eq < s["floor_sol"] and not p["active_positions"]:
            p["status"] = "floor_hit"
            s["auto_trading"] = False
            self.save_settings()
            logger.info(f"🛑 FLOOR HIT: equity {eq:.4f} SOL < {s['floor_sol']} SOL. Auto-trading stopped.")

    async def monitor_positions(self):
        """Fast loop: refresh prices of open positions and apply exit rules."""
        p, s = self.portfolio, self.settings
        if not p["active_positions"]:
            return
        pairs = await self._best_pairs(list(p["active_positions"].keys()))
        async with self._lock:
            for addr, pos in list(p["active_positions"].items()):
                pair = pairs.get(addr)
                if not pair:
                    continue
                price = _f(pair.get("priceUsd"))
                pos["current_price_usd"] = price
                if price > pos["peak_price_usd"]:
                    pos["peak_price_usd"] = price
                gain_pct = (price / pos["fill_price_usd"] - 1) * 100.0
                roi = self.position_roi_pct(pos)
                pos["current_roi_pct"] = round(roi, 2)
                pos["highest_roi_pct"] = round(max(pos["highest_roi_pct"], roi), 2)

                # 1. Stop (initial stop, breakeven after TP1, or trailing)
                if price <= pos["stop_price_usd"]:
                    label = "⚡ وقف متحرك" if pos["tp1_done"] else "🛑 وقف خسارة"
                    self._sell_fraction_locked(addr, 1.0, price, f"{label} ({gain_pct:+.1f}%)")
                    continue

                # 2. First take-profit: sell part, move stop to breakeven
                if not pos["tp1_done"] and gain_pct >= s["tp1_pct"]:
                    self._sell_fraction_locked(addr, s["tp1_sell_fraction"], price,
                                               f"🎯 هدف أول +{gain_pct:.0f}%")
                    pos["tp1_done"] = True
                    pos["stop_price_usd"] = max(pos["stop_price_usd"], pos["fill_price_usd"])

                # 3. Trail the remainder after TP1
                if pos["tp1_done"]:
                    trail = pos["peak_price_usd"] * (1 - s["trailing_distance_pct"] / 100.0)
                    pos["stop_price_usd"] = max(pos["stop_price_usd"], trail)

                # 4. Time stop: dead trade going nowhere
                held_min = (time.time() - pos["entry_timestamp"]) / 60
                if (not pos["tp1_done"] and held_min >= s["time_stop_min"]
                        and abs(gain_pct) < s["time_stop_band_pct"]):
                    self._sell_fraction_locked(addr, 1.0, price, f"⏱️ خروج زمني ({gain_pct:+.1f}%)")

            self._check_challenge_locked()
            self.save_portfolio()

    async def scan_and_trade_step(self):
        await self.update_sol_price()
        self.scanned_candidates = await self.fetch_solana_candidates()
        s, p = self.settings, self.portfolio
        if not s.get("auto_trading") or p["status"] != "running":
            return
        if len(p["active_positions"]) >= s["max_open_positions"]:
            return
        for c in self.scanned_candidates:
            if c["passed"] and c["token_address"] not in p["active_positions"]:
                if await self.execute_paper_buy(c):
                    break  # one entry per scan

    # ------------------------------------------------------------------ loops
    async def _loop(self, fn, interval_key):
        while self.is_running:
            try:
                await fn()
            except Exception as e:
                logger.error(f"{fn.__name__} error: {e}")
            await asyncio.sleep(self.settings.get(interval_key, 5))

    async def start_worker(self):
        self.is_running = True
        self.client = httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        await self.update_sol_price()
        logger.info(f"Bot v2 started | equity {self.equity_sol():.4f} SOL | goal {self.settings['goal_sol']} SOL")
        await asyncio.gather(
            self._loop(self.monitor_positions, "monitor_interval_sec"),
            self._loop(self.scan_and_trade_step, "scan_interval_sec"),
        )

    async def stop_worker(self):
        self.is_running = False
        if self.client:
            await self.client.aclose()
        logger.info("Worker stopped.")


engine = PaperTradingEngine()
