#!/usr/bin/env python3
from flask import Flask, request, jsonify
import dash
from dash import html, Input, Output, dcc
from pyngrok import ngrok
from flask import Response

import logging
import hashlib
import urllib.parse
import uuid
import requests
import json
import time
from threading import Thread
from queue import Queue
from dash import State
from dash import MATCH, ALL

PRODUCTS_FILE = "products.json"

def load_products():
    with open(PRODUCTS_FILE, "r") as f:
        return json.load(f)


# GPIO safe import (works on PC + Pi)
try:
    import RPi.GPIO as GPIO
except ImportError:
    class MockGPIO:
        BCM = OUT = HIGH = LOW = None
        def setmode(self, *a): pass
        def setwarnings(self, *a): pass
        def setup(self, *a, **k): pass
        def output(self, pin, state):
            print(f"[MOCK GPIO] Pin {pin} → {state}")
        def cleanup(self): pass
    GPIO = MockGPIO()

PORT = 5000

PAYFAST_MERCHANT_ID = "your_id"
PAYFAST_MERCHANT_KEY = "your_key"
PAYFAST_PASSPHRASE = "your_passphrase"

PAYFAST_PROCESS_URL = "https://sandbox.payfast.co.za/eng/process"
PAYFAST_VALIDATE_URL = "https://sandbox.payfast.co.za/eng/query/validate"

## Author Page, to update stock amounts ect


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "1234"  # change this!

def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD


def authenticate():
    return Response(
        "Login Required", 401,
        {"WWW-Authenticate": 'Basic realm="Login Required"'}
    )


def requires_auth(f):
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    decorated.__name__ = f.__name__
    return decorated


####
#___________________________________

PRODUCTS = load_products()

def save_products():
    with open(PRODUCTS_FILE, "w") as f:
        json.dump(PRODUCTS, f, indent=4)

ORDERS = {}

app = Flask(__name__)
server = app  # Dash needs this


##Admin Panel for stock updating
@app.route("/admin", methods=["GET", "POST"])
@requires_auth
def admin():
    if request.method == "POST":
        for sku in PRODUCTS:
            stock_val = request.form.get(f"stock_{sku}")
            if stock_val is not None:
                PRODUCTS[sku]["stock"] = int(stock_val)

            price_val = request.form.get(f"price_{sku}")
            if price_val is not None:
                PRODUCTS[sku]["price"] = f"{float(price_val):.2f}"

        # ✅ Save changes
        save_products()

    # ✅ Build HTML page (IMPORTANT: use a different variable name)
    page_html = "<h1>📦 Admin Product Manager</h1><form method='POST'>"

    for sku, product in PRODUCTS.items():
        page_html += f"""
        <div style="margin-bottom:15px; padding:10px; border:1px solid #ccc;">
            <b>{product['name']}</b><br>

            Price (R):
            <input type="number" step="0.01" name="price_{sku}" value="{product['price']}"><br>

            Stock:
            <input type="number" name="stock_{sku}" value="{product['stock']}" min="0">
        </div>
        """

    page_html += "<button type='submit'>💾 Save Changes</button></form>"

    return page_html

##___________________________

dash_app = dash.Dash(
    __name__,
    server=app,
    url_base_pathname="/",
    suppress_callback_exceptions=True,   # ✅ ADD THIS
    external_stylesheets=[
        "https://cdnjs.cloudflare.com/ajax/libs/normalize/8.0.1/normalize.min.css"
    ]
)

def get_qty_for_sku(qtys, sku_order, target_sku):
    if target_sku in sku_order:
        index = sku_order.index(target_sku)
        if index < len(qtys):
            return int(qtys[index])
    return 1

def product_card(sku, product):
    in_stock = product["stock"] > 0

    return html.Div([
        html.Div(product["name"], className="card-title"),

        html.Div(f"R {product['price']}", className="card-price"),

        html.Div(f"{product['stock']} left", className="card-stock"),

        html.Div([
            html.Button("-", id={"type": "minus", "sku": sku}, className="qty-btn"),
            html.Span("1", id={"type": "qty", "sku": sku}, className="qty-display"),
            html.Button("+", id={"type": "plus", "sku": sku}, className="qty-btn"),
        ], className="qty-container"),

        html.Button(
            "Add to Cart" if in_stock else "Out of Stock",
            id={"type": "add", "sku": sku},
            className="buy-btn",
            disabled=not in_stock
        ),

    ], className="product-card")


dash_app.layout = html.Div([

    html.Div([
        html.H1("🛒 Smart Vending Store", className="title"),
        html.P("Fast • Simple • Automated", className="subtitle")
    ], className="header"),

    html.Div([
        product_card(sku, product)
        for sku, product in PRODUCTS.items()
    ], className="grid"),

    html.Div([
        html.H2("Your Cart"),
        html.Div(id="cart-display", className="cart-box"),
        html.Div([
            html.Button("Checkout", id="checkout-btn", n_clicks=0, className="checkout-btn"),
            html.Div("\n"),
            html.Button("Clear Cart", id="clear-cart-btn", n_clicks=0, className="checkout-btn"),
        ], style={"marginTop": "10px"})
    ], className="cart-container"),

    dcc.Location(id="redirect", refresh=True),
    dcc.Store(id="cart", data={}),
    html.Div(id="debug-output", style={"display": "none"})

], className="main-container")


def generate_signature(data):
    query = urllib.parse.urlencode(data)
    if PAYFAST_PASSPHRASE:
        query += f"&passphrase={PAYFAST_PASSPHRASE}"
    return hashlib.md5(query.encode()).hexdigest()


@dash_app.callback(
    Output("cart", "data", allow_duplicate=True),  # ✅ ADD THIS
    Input("clear-cart-btn", "n_clicks"),
    prevent_initial_call=True
)
def clear_cart(n_clicks):
    if not n_clicks:
        return dash.no_update

    print("🧹 Cart cleared")
    return {}

@dash_app.callback(
    Output("redirect", "href"),
    Output("debug-output", "children"),
    Input("checkout-btn", "n_clicks"),
    State("cart", "data"),
    prevent_initial_call=True
)
def checkout(n_clicks, cart):

    if not n_clicks:
        return dash.no_update

    if not cart or len(cart) == 0:
        print("⚠️ Empty cart")
        return dash.no_update

    try:
        order_id = str(uuid.uuid4())
        total = 0
        item_names = []

        for sku, qty in cart.items():
            if sku not in PRODUCTS:
                print(f"⚠️ Invalid SKU: {sku}")
                continue

            product = PRODUCTS[sku]
            total += float(product["price"]) * qty
            item_names.append(f"{product['name']} x{qty}")

        if total == 0:
            print("⚠️ Total is zero")
            return dash.no_update

        ORDERS[order_id] = {
            "items": cart,
            "status": "pending"
        }

        data = {
            "merchant_id": PAYFAST_MERCHANT_ID,
            "merchant_key": PAYFAST_MERCHANT_KEY,
            "return_url": "http://localhost:5000/success",
            "cancel_url": "http://localhost:5000/cancel",
            "notify_url": "http://localhost:5000/itn",
            "amount": f"{total:.2f}",
            "item_name": ", ".join(item_names),
            "m_payment_id": order_id
        }

        data["signature"] = generate_signature(data)

        payment_url = PAYFAST_PROCESS_URL + "?" + urllib.parse.urlencode(data)

        print("✅ Redirecting:", payment_url)

        return payment_url, "ok"

    except Exception as e:
        print("❌ Checkout crashed:", e)
        return dash.no_update, "error"
    

@dash_app.callback(
    Output("cart-display", "children"),
    Input("cart", "data")
)
def display_cart(cart):
    if not cart:
        return "Cart is empty"

    items = []
    total = 0

    for sku, qty in cart.items():
        product = PRODUCTS[sku]
        price = float(product["price"])
        subtotal = price * qty
        total += subtotal

        items.append(html.Div(
            f"{product['name']} x {qty} = R {subtotal:.2f}"
        ))

    items.append(html.H3(f"Total: R {total:.2f}"))

    return items

@dash_app.callback(
    Output("cart", "data"),
    Input({"type": "add", "sku": ALL}, "n_clicks"),
    State("cart", "data"),
    State({"type": "qty", "sku": ALL}, "children"),
    prevent_initial_call=True
)
def add_to_cart(clicks, cart, qtys):

    ctx = dash.callback_context

    if not ctx.triggered:
        return dash.no_update

    if cart is None:
        cart = {}

    # ✅ Identify clicked button
    trigger = ctx.triggered[0]["prop_id"].split(".")[0]
    trigger_dict = json.loads(trigger)
    clicked_sku = trigger_dict["sku"]

    sku_order = list(PRODUCTS.keys())

    # ✅ Get requested quantity
    qty = get_qty_for_sku(qtys, sku_order, clicked_sku)

    # ✅ Current cart quantity
    current_in_cart = cart.get(clicked_sku, 0)

    # ✅ Available stock
    available_stock = PRODUCTS[clicked_sku]["stock"]

    # ✅ Remaining stock that can still be added
    remaining_stock = available_stock - current_in_cart

    # ✅ Prevent over-ordering
    if remaining_stock <= 0:
        print(f"⚠️ No more stock available for {clicked_sku}")
        return cart

    # Cap quantity to remaining stock
    qty_to_add = min(qty, remaining_stock)

    # ✅ Update cart
    cart[clicked_sku] = current_in_cart + qty_to_add

    print("✅ Cart updated:", cart)

    return cart

@dash_app.callback(
    Output({"type": "qty", "sku": MATCH}, "children"),
    Input({"type": "plus", "sku": MATCH}, "n_clicks"),
    Input({"type": "minus", "sku": MATCH}, "n_clicks"),
    State({"type": "qty", "sku": MATCH}, "children")
)
def update_qty(plus, minus, current):
    plus = plus or 0
    minus = minus or 0
    qty = int(current)

    if plus > minus:
        qty += 1
    elif minus > plus:
        qty = max(1, qty - 1)

    return str(qty)

@dash_app.callback(
    Output("redirect", "href"),
    [Input(f"buy-{sku}", "n_clicks") for sku in PRODUCTS.keys()]
)
def handle_buy(*clicks):
    ctx = dash.callback_context

    if not ctx.triggered:
        return dash.no_update

    button_id = ctx.triggered[0]["prop_id"].split(".")[0]
    sku = button_id.replace("buy-", "")

    product = PRODUCTS.get(sku)

    order_id = str(uuid.uuid4())

    ORDERS[order_id] = {"sku": sku, "status": "pending"}

    data = {
        "merchant_id": PAYFAST_MERCHANT_ID,
        "merchant_key": PAYFAST_MERCHANT_KEY,
        "return_url": "http://localhost:5000/success",
        "cancel_url": "http://localhost:5000/cancel",
        "notify_url": "https://your-ngrok-url/itn",
        "amount": product["price"],
        "item_name": product["name"],
        "m_payment_id": order_id
    }

    data["signature"] = generate_signature(data)

    return PAYFAST_PROCESS_URL + "?" + urllib.parse.urlencode(data)

GPIO.setmode(GPIO.BCM)

PIN_MAP = {
    "R1": 4,
    "R2": 17,
    "R3": 27,
}

task_queue = Queue()

def queue_worker():
    while True:
        sku = task_queue.get()

        pin = PIN_MAP.get(sku)
        if pin:
            print(f"Dispensing {sku} → pin {pin}")
            GPIO.output(pin, GPIO.LOW)
            time.sleep(2)
            GPIO.output(pin, GPIO.HIGH)

        task_queue.task_done()

Thread(target=queue_worker, daemon=True).start()

@app.route("/itn", methods=["POST"])
def itn():
    data = request.form.to_dict()

    received_sig = data.pop("signature", None)

    if generate_signature(data) != received_sig:
        return "Invalid signature", 400

    response = requests.post(PAYFAST_VALIDATE_URL, data=request.form)

    if response.text != "VALID":
        return "Invalid", 400

    if data.get("payment_status") == "COMPLETE":
        order_id = data.get("m_payment_id")


        
        order = ORDERS.get(order_id, {})
        items = order.get("items", {})  # multiple items now

        if items:
            for sku, qty in items.items():
                if sku in PRODUCTS:
                    PRODUCTS[sku]["stock"] -= qty

                for _ in range(qty):
                    task_queue.put(sku)

            save_products()


    return "OK"

@app.route("/success")
def success():
    return "✅ Payment successful!"

@app.route("/cancel")
def cancel():
    return "❌ Payment cancelled"


if __name__ == "__main__":
    print("Starting app...")

    # Optional ngrok
    # ngrok.connect(PORT)

    app.run(host="0.0.0.0", port=PORT, debug=True, use_reloader=False)