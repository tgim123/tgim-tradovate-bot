import os, time, uuid, threading
from typing import Optional, Dict, Any
from flask import Flask, request, jsonify
import requests

# ─────────────────────────────────────────────────────────────
# Environment configuration (ALL set in Render → Environment)
# ─────────────────────────────────────────────────────────────
TDV_HOST         = os.getenv("TDV_HOST", "https://demo.tradovateapi.com")  # NO trailing /v1 here
TDV_USERNAME     = os.getenv("TDV_USERNAME")
TDV_PASSWORD     = os.getenv("TDV_PASSWORD")
TDV_APP_ID       = os.getenv("TDV_APP_ID", "TGIMBot")
TDV_APP_VERSION  = os.getenv("TDV_APP_VERSION", "1.0")
TDV_CID          = int(os.getenv("TDV_CID", "8"))
TDV_DEVICE_ID    = os.getenv("TDV_DEVICE_ID", str(uuid.uuid4()))
TDV_SEC          = os.getenv("TDV_SEC")
TDV_ACCOUNT_ID   = os.getenv("TDV_ACCOUNT_ID")  # optional; will auto-pick first account if missing
TV_SHARED_SECRET = os.getenv("TV_SHARED_SECRET", "")  # optional; matches header X-TV-Secret or ?secret=

HTTP_TIMEOUT_S   = 15

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
# Tradovate REST client
# ─────────────────────────────────────────────────────────────
class TradovateClient:
    def __init__(self):
        self.session = requests.Session()
        self._token: Optional[str] = None
        self._md_token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._account_id: Optional[int] = int(TDV_ACCOUNT_ID) if TDV_ACCOUNT_ID else None
        self._contract_cache: Dict[str, int] = {}
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
            self._md_token = data.get("mdAccessToken")
            # fallback validity window ~23h if expiry not provided
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
            raise RuntimeError("No Tradovate accounts available for these credentials.")
        return int(accounts[0]["id"])  # pick first account by default

    def resolve_contract_id(self, symbol: str) -> int:
        """Resolve a contract name like '6CZ5' → contractId. Requires explicit month (no '!')."""
        if "!" in symbol:
            raise ValueError(f"Use explicit month (e.g., 6CZ5). Continuous symbols like '{symbol}' are not supported.")
        if symbol in self._contract_cache:
            return self._contract_cache[symbol]
        url = f"{TDV_HOST}/v1/contract/find"
        r = self.session.get(url, headers=self._headers(), params={"name": symbol}, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("id"):
            cid = int(data["id"])
        elif isinstance(data, list) and data:
            cid = int(data[0]["id"])
        else:
            raise RuntimeError(f"Could not resolve contractId for '{symbol}'.")
        self._contract_cache[symbol] = cid
        return cid

    def place_market(self, symbol: str, qty: int, side: str) -> Dict[str, Any]:
        if qty <= 0:
            raise ValueError("Quantity must be > 0")
        cid = self.resolve_contract_id(symbol)
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

    def flatten_side(self, side: str) -> Dict[str, Any]:
        side = side.lower()
        url_pos = f"{TDV_HOST}/v1/position/list"
        r = self.session.get(url_pos, headers=self._headers(), timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        positions = r.json() if isinstance(r.json(), list) else []

        targets = []
        for p in positions:
            net = int(p.get("netPos", 0))
            if side == "long" and net > 0:
                targets.append((p["contractId"], net, "SELL"))
            elif side == "short" and net < 0:
                targets.append((p["contractId"], abs(net), "BUY"))

        results = []
        for contract_id, qty, action in targets:
            body = {
                "accountId": self._account_id,
                "contractId": contract_id,
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
# Routes
# ─────────────────────────────────────────────────────────────
@app.get("/")
def health():
    return jsonify({"ok": True, "service": "TGIM Tradovate Bridge", "host": TDV_HOST})

@app.post("/webhook")
def webhook():
    # Optional shared secret (either header or query param)
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
            instrument = str(payload.get("instrument", "")).strip()
            if not instrument:
                return jsonify({"ok": False, "error": "instrument required"}), 400
            units = int(float(payload.get("units", 1)))
            resp = client.place_market(instrument, units, action)
            return jsonify({"ok": True, "type": "entry", "action": action, "instrument": instrument, "resp": resp})

        if action == "close":
            side = str(payload.get("side", "")).lower()
            if side not in ("long", "short"):
                return jsonify({"ok": False, "error": "close requires side=long|short"}), 400
            resp = client.flatten_side(side)
            return jsonify({"ok": True, "type": "close", "side": side, "resp": resp})

        return jsonify({"ok": False, "error": f"unknown action '{action}'"}), 400

    except requests.HTTPError as he:
        try:
            return jsonify({"ok": False, "http": he.response.status_code, "body": he.response.json()}), 502
        except Exception:
            return jsonify({"ok": False, "http": 502, "body": str(he)}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# Entrypoint for local runs; Render uses Procfile (gunicorn app:app)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
