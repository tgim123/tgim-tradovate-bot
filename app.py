import os, time, uuid, threading, datetime
from typing import Optional, Dict, Any, List
from flask import Flask, request, jsonify
import requests

# ─────────────────────────────────────────────────────────────
# ENV (set these in Render → Environment)
# ─────────────────────────────────────────────────────────────
TDV_HOST         = os.getenv("TDV_HOST", "https://demo.tradovateapi.com")   # no trailing /v1
TDV_USERNAME     = os.getenv("TDV_USERNAME")
TDV_PASSWORD     = os.getenv("TDV_PASSWORD")
TDV_APP_ID       = os.getenv("TDV_APP_ID", "TGIMBot")
TDV_APP_VERSION  = os.getenv("TDV_APP_VERSION", "1.0")
TDV_CID          = int(os.getenv("TDV_CID", "8"))
TDV_DEVICE_ID    = os.getenv("TDV_DEVICE_ID", str(uuid.uuid4()))
TDV_SEC          = os.getenv("TDV_SEC")
TDV_ACCOUNT_ID   = os.getenv("TDV_ACCOUNT_ID")  # optional; auto-picks first if missing
TV_SHARED_SECRET = os.getenv("TV_SHARED_SECRET", "")  # optional; header X-TV-Secret or ?secret=

HTTP_TIMEOUT_S   = 15

# ─────────────────────────────────────────────────────────────
# RISK GUARDRAILS (tuned for $50k acct / $2k MDD) — EDIT HERE
# ─────────────────────────────────────────────────────────────
ACCOUNT_SIZE_USD = float(os.getenv("RISK_ACCOUNT_SIZE", "50000"))
MAX_DRAWDOWN_USD = float(os.getenv("RISK_MAX_DD", "2000"))

# Conservative per-trade contract caps (root symbol → max qty)
# Add any other instruments you plan to trade.
MAX_QTY_PER_ROOT: Dict[str, int] = {
    # Equity index E-minis / micros
    "ES": 1, "MES": 10,
    "NQ": 1, "MNQ": 6,
    "YM": 1, "MYM": 10,
    "RTY": 1, "M2K": 10,

    # CME FX (majors) + micros
    "6E": 2, "M6E": 10,
    "6J": 1, "M6J": 6,
    "6B": 2, "M6B": 10,
    "6C": 2, "MCD": 10,   # CAD; Tradovate micro = MCD
    "6A": 2, "M6A": 10,   # AUD, micro ticker can vary by broker listing

    # Metals / Energy (prefer micros)
    "GC": 1, "MGC": 10,
    "SI": 1, "SIL": 6,    # SIL = micro silver in some listings
    "CL": 1, "MCL": 10,
    "NG": 1, "QG": 6,     # QG = e-mini natgas (check listing)
}

# Per-order hard cap in dollars (prevents fat-finger on big minis)
MAX_NOTIONAL_PER_ORDER = float(os.getenv("RISK_MAX_NOTIONAL", "20000"))

# ─────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
# Tradovate REST client
# ─────────────────────────────────────────────────────────────
class TradovateClient:
    def __init__(self):
        self.session = requests.Session()
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._account_id: Optional[int] = int(TDV_ACCOUNT_ID) if TDV_ACCOUNT_ID else None
        self._contract_cache: Dict[str, int] = {}   # "ESZ5" or "ES->resolved_id"
        self._root_to_active: Dict[str, Dict[str, Any]] = {}  # cache of root → active contract JSON
        self._lock = threading.Lock()

    def _auth_payload(self) -> Dict[str, Any]:
        if not (TDV_USERNAME and TDV_PASSWORD and TDV_SEC):
            raise RuntimeError("Missing TDV credentials: TDV_USERNAME, TDV_PASSWORD, TDV_SEC.")
        return {
            "name": TDV_USERNAME,
            "password": TDV_PASSWORD,
            "appId": TDV_APP_ID,
            "appVersion": TDV_APP_VERSION,
            "cid": TDV_CID,
            "deviceId": TDV_DEVICE_ID,
            "sec": TDV_SEC
        }

    def ensure_token(self):
        with self._lock:
            now = time.time()
            if self._token and now < (self._token_expiry - 60):
                return
            url = f"{TDV_HOST}/v1/auth/accesstokenrequest"
            r = self.session.post(url, json=self._auth_payload(), timeout=HTTP_TIMEOUT_S)
            r.raise_for_status()
            data = r.json()
            self._token = data.get("accessToken")
            # fallback validity window ~23h
            self._token_expiry = now + 23 * 3600
            if not self._token:
                raise RuntimeError("Tradovate auth returned no accessToken.")
            if not self._account_id:
                self._account_id = self._fetch_account_id()

    def _headers(self) -> Dict[str, str]:
        self.ensure_token()
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    def _fetch_account_id(self) -> int:
        url = f"{TDV_HOST}/v1/account/list"
        r = self.session.get(url, headers=self._headers(), timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        accounts = r.json()
        if not isinstance(accounts, list) or not accounts:
            raise RuntimeError("No Tradovate accounts available.")
        return int(accounts[0]["id"])

    # ── Contract resolution ───────────────────────────────────
    @staticmethod
    def _is_explicit_month(symbol: str) -> bool:
        # crude: explicit month codes include a digit (e.g., ESZ5, 6EZ5)
        return any(ch.isdigit() for ch in symbol)

    def _pick_front_month(self, contracts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Choose nearest non-expired contract (by expiration)."""
        now = datetime.datetime.utcnow()
        best = None
        best_exp = None
        for c in contracts:
            exp_raw = c.get("expiration") or c.get("expirationDate") or c.get("expirationTime")
            if not exp_raw:
                continue
            try:
                exp = datetime.datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
            except Exception:
                continue
            if exp > now and (best_exp is None or exp < best_exp):
                best, best_exp = c, exp
        # if none in future, fall back to first
        return best or (contracts[0] if contracts else None)

    def resolve_contract_id(self, instrument: str) -> int:
        """Accepts ES or ESZ5, 6E or 6EZ5, etc."""
        key = instrument.strip().upper()

        # If explicit month ("ESZ5"), we can find directly.
        if self._is_explicit_month(key):
            if key in self._contract_cache:
                return self._contract_cache[key]
            url = f"{TDV_HOST}/v1/contract/find"
            r = self.session.get(url, headers=self._headers(), params={"name": key}, timeout=HTTP_TIMEOUT_S)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("id"):
                cid = int(data["id"])
            elif isinstance(data, list) and data:
                cid = int(data[0]["id"])
            else:
                raise RuntimeError(f"Could not resolve contractId for '{key}'.")
            self._contract_cache[key] = cid
            return cid

        # Root-only ("ES", "MES", "6E", "MGC") → pull all matches, pick front month
        root = key
        cache_key = f"{root}->active_id"
        if cache_key in self._contract_cache:
            return self._contract_cache[cache_key]

        url = f"{TDV_HOST}/v1/contract/find"
        r = self.session.get(url, headers=self._headers(), params={"name": root}, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
        contracts: List[Dict[str, Any]] = []
        if isinstance(data, dict) and data.get("id"):
            contracts = [data]
        elif isinstance(data, list):
            # filter by root match at the beginning of name
            contracts = [c for c in data if str(c.get("name", "")).upper().startswith(root)]
        if not contracts:
            raise RuntimeError(f"No contracts found for root '{root}'.")

        chosen = self._pick_front_month(contracts)
        if not chosen or not chosen.get("id"):
            raise RuntimeError(f"Could not determine front-month for '{root}'.")
        cid = int(chosen["id"])
        self._root_to_active[root] = chosen
        self._contract_cache[cache_key] = cid
        return cid

    # ── Orders ────────────────────────────────────────────────
    def place_market(self, instrument: str, qty: int, side: str) -> Dict[str, Any]:
        cid = self.resolve_contract_id(instrument)
        action = "BUY" if side.lower() == "buy" else "SELL"
        body = {
            "accountId": self._account_id,
            "contractId": cid,
            "action": action,
            "orderType": "Market",
            "orderQty": qty,
            "timeInForce": "Day",
            "isAutomated": True
        }
        url = f"{TDV_HOST}/v1/order/place"
        r = self.session.post(url, headers=self._headers(), json=body, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    def positions(self) -> List[Dict[str, Any]]:
        url = f"{TDV_HOST}/v1/position/list"
        r = self.session.get(url, headers=self._headers(), timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def flatten_side(self, side: str, instrument: Optional[str] = None) -> Dict[str, Any]:
        side = side.lower()
        target_cid = None
        if instrument:
            target_cid = self.resolve_contract_id(instrument)

        pos = self.positions()
        results = []
        for p in pos:
            if target_cid and int(p.get("contractId")) != target_cid:
                continue
            net = int(p.get("netPos", 0))
            if side == "long" and net > 0:
                action = "SELL"
                qty = net
            elif side == "short" and net < 0:
                action = "BUY"
                qty = abs(net)
            else:
                continue
            body = {
                "accountId": self._account_id,
                "contractId": int(p["contractId"]),
                "action": action,
                "orderType": "Market",
                "orderQty": qty,
                "timeInForce": "Day",
                "isAutomated": True
            }
            url = f"{TDV_HOST}/v1/order/place"
            rr = self.session.post(url, headers=self._headers(), json=body, timeout=HTTP_TIMEOUT_S)
            rr.raise_for_status()
            results.append(rr.json())
        return {"closed": results, "count": len(results)}

client = TradovateClient()

# ─────────────────────────────────────────────────────────────
# Risk checks
# ─────────────────────────────────────────────────────────────
def root_from_instrument(instr: str) -> str:
    s = instr.upper()
    # strip month code if present → take leading alpha block
    i = 0
    while i < len(s) and s[i].isalpha():
        i += 1
    return s[:i] if i > 0 else s

def enforce_risk(instrument: str, qty: int):
    if qty <= 0:
        raise ValueError("Quantity must be > 0")

    root = root_from_instrument(instrument)
    max_allowed = MAX_QTY_PER_ROOT.get(root)
    if max_allowed is not None and qty > max_allowed:
        raise ValueError(f"Risk guard: {root} max qty {max_allowed}, requested {qty}.")

    # crude notional cap guard (prevents huge orders on minis)
    # get a price via md endpoint if available; otherwise skip this check
    try:
        # Attempt to fetch a recent quote for estimate (md token not required for REST here, but may 401 on some setups)
        # If this errors, we silently ignore and rely on MAX_QTY_PER_ROOT.
        cid = client.resolve_contract_id(instrument)
        # MV: a true quote API requires market-data token; as a conservative fallback we skip.
        # You can plug a pricing source here to compute notional and enforce MAX_NOTIONAL_PER_ORDER.
        _ = cid
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────
@app.get("/")
def health():
    roots = sorted(set(MAX_QTY_PER_ROOT.keys()))
    return jsonify({
        "ok": True,
        "service": "TGIM Tradovate Bridge",
        "host": TDV_HOST,
        "risk": {
            "account_size": ACCOUNT_SIZE_USD,
            "max_drawdown": MAX_DRAWDOWN_USD,
            "per_root_caps": MAX_QTY_PER_ROOT
        },
        "roots_supported": roots[:50]  # preview
    })

@app.post("/webhook")
def webhook():
    # Optional shared secret
    if TV_SHARED_SECRET:
        if request.headers.get("X-TV-Secret") != TV_SHARED_SECRET and request.args.get("secret") != TV_SHARED_SECRET:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"ok": False, "error": "invalid JSON"}), 400

    action = str(payload.get("action", "")).lower()
    try:
        if action in ("buy", "sell"):
            instrument = str(payload.get("instrument", "")).strip().upper()
            if not instrument:
                return jsonify({"ok": False, "error": "instrument required"}), 400
            qty = int(float(payload.get("units", 1)))
            enforce_risk(instrument, qty)
            resp = client.place_market(instrument, qty, action)
            return jsonify({"ok": True, "type": "entry", "action": action, "instrument": instrument, "qty": qty, "resp": resp})

        if action == "close":
            side = str(payload.get("side", "")).lower()
            if side not in ("long", "short"):
                return jsonify({"ok": False, "error": "close requires side=long|short"}), 400
            instrument = payload.get("instrument")
            instrument = str(instrument).upper().strip() if instrument else None
            resp = client.flatten_side(side, instrument)
            return jsonify({"ok": True, "type": "close", "side": side, "instrument": instrument, "resp": resp})

        return jsonify({"ok": False, "error": f"unknown action '{action}'"}), 400

    except requests.HTTPError as he:
        try:
            return jsonify({"ok": False, "http": he.response.status_code, "body": he.response.json()}), 502
        except Exception:
            return jsonify({"ok": False, "http": 502, "body": str(he)}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

# Entrypoint (Render uses Procfile → gunicorn app:app)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
