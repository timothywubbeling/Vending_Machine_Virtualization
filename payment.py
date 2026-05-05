# payment.py
import hashlib
import urllib.parse

MERCHANT_ID = "your_id"
MERCHANT_KEY = "your_key"
PASSPHRASE = "your_passphrase"  # optional but recommended

PAYFAST_URL = "https://www.payfast.co.za/eng/process"

def generate_signature(data):
    query = urllib.parse.urlencode(data)
    if PASSPHRASE:
        query += f"&passphrase={PASSPHRASE}"
    return hashlib.md5(query.encode()).hexdigest()


def create_payment(order):
    data = {
        "merchant_id": MERCHANT_ID,
        "merchant_key": MERCHANT_KEY,
        "return_url": "https://yourdomain/success",
        "cancel_url": "https://yourdomain/cancel",
        "notify_url": "https://yourdomain/itn",
        "amount": order["amount"],
        "item_name": order["item_name"],
        "m_payment_id": order["id"]
    }

    data["signature"] = generate_signature(data)

    return PAYFAST_URL + "?" + urllib.parse.urlencode(data)