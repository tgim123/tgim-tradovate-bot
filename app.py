from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

TRADOVATE_URL = os.getenv("TDV_HOST", "https://demo.tradovateapi.com/v1")
TRADOVATE_USERNAME = os.getenv("TDV_USERNAME")
TRADOVATE_PASSWORD = os.getenv("TDV_PASSWORD")
TRADOVATE_APP_ID = os.getenv("TDV_APP_ID", "Sample App")
TRADOVATE_APP_VERSION = os.getenv("TDV_APP_VERSION", "1.0")
TRADOVATE_CID = os.getenv("TDV_CID", "8")
TRADOVATE_SEC = os.getenv("TDV_SEC")
TRADOVATE_DEVICE_ID = os.getenv("TDV_DEVICE_ID")

token_cache = {}

def get_access_token():
    if "token" in token_cache:
        return token_cache["token"]

    payload = {
        "name": TRADOVATE_USERNAME,
        "password": TRADOVATE_PASSWORD,
        "appId": TRADOVATE_APP_ID,
        "appVersion": TRADOVATE_APP_VERSION,
        "cid": TRADOVATE_CID,
        "sec": TRADOVATE_SEC,
        "deviceId": TRADOVATE_DEVICE_ID,
    }
    r = requests.post(f"{TRADOVATE_URL}/auth/accesstokenrequest", json=payload)
    data = r.json()
    token_cache["token"] = data["accessToken"]
    return data["accessToken"]

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    action = data.get("action")
    instrument = data.get("instrument")
    qty = data.get("units", 1)

    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    if action in ["buy", "sell"]:
        side = "BUY" if action == "buy" else "SELL"
        order = {
            "accountId": int(os.getenv("TDV_ACCOUNT_ID")),
            "action": side,
            "symbol": instrument,
            "orderQty": qty,
            "orderType": "Market"
        }
        r = requests.post(f"{TRADOVATE_URL}/order/placeorder", headers=headers, json=order)
        return jsonify(r.json())
    return jsonify({"error": "Invalid action"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
