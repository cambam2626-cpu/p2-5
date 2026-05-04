"""
Young's Restaurant — FastAPI backend.
Full feature parity with the gRPC RestaurantService spec.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
import time

app = FastAPI(title="Young's Restaurant API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory state
# ─────────────────────────────────────────────────────────────────────────────

users = {
    "manager": {"password": "pw", "role": "MANAGER"},
    "server1": {"password": "pw", "role": "SERVER"},
    "server2": {"password": "pw", "role": "SERVER"},
    "chef":    {"password": "pw", "role": "CHEF"},
}

sessions = {}

orders = {}
next_order_id = 1000

kitchen_queue = []
next_kitchen_item_id = 1

tables = {
    1: {"capacity": 2, "occupied": False, "order_id": None},
    2: {"capacity": 2, "occupied": False, "order_id": None},
    3: {"capacity": 4, "occupied": False, "order_id": None},
    4: {"capacity": 4, "occupied": False, "order_id": None},
    5: {"capacity": 6, "occupied": False, "order_id": None},
}

waitlist = []
next_waitlist_id = 1

menu = {
    "Fried Pickles":            {"category": "STARTER", "price": 6.99,  "available": True},
    "Wings":                    {"category": "STARTER", "price": 9.99,  "available": True},
    "Stuffed Mushrooms":        {"category": "STARTER", "price": 7.99,  "available": True},

    "Cheeseburger":             {"category": "MAIN",    "price": 12.99, "available": True},
    "Chili Cheese Dog":         {"category": "MAIN",    "price": 10.99, "available": True},
    "Cuban Sandwich":           {"category": "MAIN",    "price": 13.99, "available": True},
    "Full Rack Ribs":           {"category": "MAIN",    "price": 18.99, "available": True},
    "Buffalo Chicken Sandwich": {"category": "MAIN",    "price": 11.99, "available": True},

    "Chocolate Cake":           {"category": "DESSERT", "price": 6.99,  "available": True},
    "Banana Split":             {"category": "DESSERT", "price": 7.99,  "available": True},
    "Honey Ice Cream":          {"category": "DESSERT", "price": 5.99,  "available": True},

    "Coke":      {"category": "DRINK", "price": 2.99, "available": True},
    "Sprite":    {"category": "DRINK", "price": 2.99, "available": True},
    "Fanta":     {"category": "DRINK", "price": 2.99, "available": True},
    "Water":     {"category": "DRINK", "price": 0.00, "available": True},
    "Milkshake": {"category": "DRINK", "price": 4.99, "available": True},
}


# ─────────────────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class LogoutRequest(BaseModel):
    session_token: str

class UpdatePriceRequest(BaseModel):
    session_token: str
    item_name: str
    new_price: float

class CreateOrderRequest(BaseModel):
    session_token: str
    order_type: str             # "dine_in" | "take_out"
    table_number: int = 0
    guests: int = 0
    customer_name: str = ""

class AddItemRequest(BaseModel):
    session_token: str
    order_id: int
    item_name: str
    quantity: int
    seat_number: int = 0

class RemoveItemRequest(BaseModel):
    session_token: str
    order_id: int
    item_name: str
    quantity: int
    seat_number: int = 0

class MarkOrderReadyRequest(BaseModel):
    session_token: str
    order_id: int

class CompleteKitchenItemRequest(BaseModel):
    session_token: str
    kitchen_item_id: int

class CashoutRequest(BaseModel):
    session_token: str
    order_id: int
    payment_target: str         # "SEAT" | "TABLE" | "TAKEOUT_ORDER"
    seat_number: int = 0

class AddToWaitlistRequest(BaseModel):
    session_token: str
    customer_name: str
    party_size: int

class SeatWaitlistPartyRequest(BaseModel):
    session_token: str
    waitlist_id: int
    table_number: int


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def money(value):
    return Decimal(str(value))

def round_money(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def normalize_item_name(item_name):
    if not item_name:
        return None
    clean = item_name.strip().lower()
    for name in menu:
        if name.lower() == clean:
            return name
    return None

def is_kitchen_item(item_name):
    return item_name in menu and menu[item_name]["category"] != "DRINK"

def get_session(token):
    return sessions.get(token)

def require_role(token, allowed_roles):
    sess = get_session(token)
    if not sess:
        return None, "Unauthorized — please sign in again"
    if sess["role"] not in allowed_roles:
        return None, "Forbidden — your role can't perform this action"
    return sess, None

def order_total(order):
    total = Decimal("0.00")
    for it in order["items"]:
        total += money(it["unit_price"]) * it["quantity"]
    return total

def seat_total(order, seat_number):
    total = Decimal("0.00")
    for it in order["items"]:
        if it["seat_number"] == seat_number:
            total += money(it["unit_price"]) * it["quantity"]
    return total

def count_qty(order, item_name, seat_number):
    return sum(it["quantity"] for it in order["items"]
               if it["item_name"] == item_name and it["seat_number"] == seat_number)

def remove_lines(order, item_name, quantity, seat_number):
    remaining = quantity
    # iterate over a copy because we mutate
    for line in list(reversed(order["items"])):
        if remaining <= 0:
            break
        if line["item_name"] == item_name and line["seat_number"] == seat_number:
            if line["quantity"] <= remaining:
                remaining -= line["quantity"]
                order["items"].remove(line)
            else:
                line["quantity"] -= remaining
                line["line_total"] = round_money(money(line["unit_price"]) * line["quantity"])
                remaining = 0
    return remaining == 0

def add_to_kitchen(order_id, order, item_name, quantity, seat_number):
    global next_kitchen_item_id
    if not is_kitchen_item(item_name):
        return
    kitchen_queue.append({
        "kitchen_item_id": next_kitchen_item_id,
        "order_id":        order_id,
        "table_number":    order["table_number"],
        "seat_number":     seat_number,
        "customer_name":   order.get("customer_name", ""),
        "item_name":       item_name,
        "quantity":        quantity,
        "timestamp":       now_str(),
        "created_at":      time.time(),
        "status":          "PENDING",
    })
    next_kitchen_item_id += 1

def remove_from_kitchen(order_id, item_name, quantity, seat_number):
    """When a server pulls items off an order, mark matching pending kitchen rows complete (LIFO)."""
    remaining = quantity
    pending = sorted(
        [k for k in kitchen_queue
         if k["order_id"]    == order_id
         and k["item_name"]  == item_name
         and k["seat_number"] == seat_number
         and k["status"]     == "PENDING"],
        key=lambda x: (x["created_at"], x["kitchen_item_id"]),
        reverse=True,
    )
    for k in pending:
        if remaining <= 0:
            break
        if k["quantity"] <= remaining:
            remaining -= k["quantity"]
            k["status"] = "COMPLETED"
        else:
            k["quantity"] -= remaining
            remaining = 0

def release_table(table_number):
    if table_number in tables:
        tables[table_number]["occupied"] = False
        tables[table_number]["order_id"] = None

def occupy_table(table_number, order_id):
    tables[table_number]["occupied"] = True
    tables[table_number]["order_id"] = order_id

def recompute_status(order_id):
    """PLACED → READY → PARTIALLY_PAID → PAID lifecycle."""
    order = orders[order_id]
    total = order_total(order)
    paid  = money(order.get("paid", 0.0))

    has_pending = any(
        k["order_id"] == order_id and k["status"] == "PENDING"
        for k in kitchen_queue
    )

    if total > Decimal("0.00") and paid >= total:
        order["status"] = "PAID"
        if order["type"] == "dine_in":
            release_table(order["table_number"])
        return

    if paid > Decimal("0.00"):
        order["status"] = "PARTIALLY_PAID"
        return

    order["status"] = "PLACED" if has_pending else "READY"

def seat_kitchen_status(order_id, seat_number, item_name):
    has_pending = False
    has_completed = False
    for k in kitchen_queue:
        if (k["order_id"] == order_id
            and k["seat_number"] == seat_number
            and k["item_name"] == item_name):
            if k["status"] == "PENDING":
                has_pending = True
            elif k["status"] == "COMPLETED":
                has_completed = True
    if has_pending:
        return True, "PENDING"
    if has_completed:
        return True, "COMPLETED"
    return False, "COMPLETED"


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def home():
    return {"message": "Young's Restaurant backend is running", "endpoints": "see /docs"}


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.post("/login")
def login(req: LoginRequest):
    user = users.get(req.username)
    if not user or user["password"] != req.password:
        return {"success": False, "message": "Invalid credentials"}
    token = f"{req.username}_session"
    sessions[token] = {"username": req.username, "role": user["role"]}
    return {
        "success": True,
        "message": "Login successful",
        "session_token": token,
        "role": user["role"],
        "username": req.username,
    }

@app.post("/logout")
def logout(req: LogoutRequest):
    if req.session_token in sessions:
        del sessions[req.session_token]
        return {"success": True, "message": "Logged out"}
    return {"success": False, "message": "Invalid session"}


# ── Menu ─────────────────────────────────────────────────────────────────────

@app.get("/menu")
def get_menu():
    return {"success": True, "menu": menu}

@app.post("/menu/update-price")
def update_price(req: UpdatePriceRequest):
    _, err = require_role(req.session_token, {"MANAGER"})
    if err:
        return {"success": False, "message": err}
    name = normalize_item_name(req.item_name)
    if name is None:
        return {"success": False, "message": "Invalid menu item"}
    if req.new_price < 0:
        return {"success": False, "message": "Price cannot be negative"}
    menu[name]["price"] = round_money(money(req.new_price))
    return {"success": True, "message": f"{name} updated to ${menu[name]['price']:.2f}"}


# ── Tables ───────────────────────────────────────────────────────────────────

@app.get("/tables")
def get_tables():
    return {"success": True, "tables": tables}


# ── Orders ───────────────────────────────────────────────────────────────────

@app.post("/orders")
def create_order(req: CreateOrderRequest):
    global next_order_id

    _, err = require_role(req.session_token, {"MANAGER", "SERVER"})
    if err:
        return {"success": False, "message": err}

    clean = req.order_type.strip().lower().replace(" ", "_")
    if clean not in ("dine_in", "take_out"):
        return {"success": False, "message": "Invalid order type"}

    if clean == "dine_in":
        if req.table_number not in tables:
            return {"success": False, "message": "Table does not exist"}
        if tables[req.table_number]["occupied"]:
            return {"success": False, "message": "Table is occupied"}
        cap = tables[req.table_number]["capacity"]
        if req.guests < 1 or req.guests > cap:
            return {"success": False, "message": f"Guest count must be 1–{cap} for this table"}

    if clean == "take_out":
        if not req.customer_name.strip():
            return {"success": False, "message": "Customer name required for take-out"}

    next_order_id += 1
    oid = next_order_id

    orders[oid] = {
        "order_id":      oid,
        "type":          clean,
        "status":        "PLACED",
        "table_number":  req.table_number if clean == "dine_in" else 0,
        "guests":        req.guests if clean == "dine_in" else 0,
        "customer_name": req.customer_name.strip() if clean == "take_out" else "",
        "items":         [],
        "paid":          0.0,
        "seat_paid":     {},
        "created_at":    now_str(),
    }

    if clean == "dine_in":
        occupy_table(req.table_number, oid)

    return {"success": True, "message": "Order created", "order_id": oid}


@app.post("/orders/add-item")
def add_item(req: AddItemRequest):
    _, err = require_role(req.session_token, {"MANAGER", "SERVER"})
    if err:
        return {"success": False, "message": err}

    if req.order_id not in orders:
        return {"success": False, "message": "Order not found"}
    if req.quantity < 1:
        return {"success": False, "message": "Quantity must be at least 1"}

    order = orders[req.order_id]
    name = normalize_item_name(req.item_name)
    if name is None:
        return {"success": False, "message": "Invalid menu item"}
    if not menu[name]["available"]:
        return {"success": False, "message": "Item is unavailable"}
    if order["status"] == "PAID":
        return {"success": False, "message": "Cannot modify a paid order"}

    if order["type"] == "dine_in":
        if req.seat_number < 1 or req.seat_number > order["guests"]:
            return {"success": False, "message": f"Seat must be 1–{order['guests']}"}
    else:
        if req.seat_number != 0:
            return {"success": False, "message": "Seat must be 0 for take-out"}
        # take-out cap
        current = sum(i["quantity"] for i in order["items"])
        if current + req.quantity > 10:
            return {"success": False, "message": "Take-out limit is 10 items"}

    unit_price = round_money(money(menu[name]["price"]))
    order["items"].append({
        "item_name":   name,
        "quantity":    req.quantity,
        "unit_price":  unit_price,
        "seat_number": req.seat_number,
        "line_total":  round_money(money(unit_price) * req.quantity),
    })

    add_to_kitchen(req.order_id, order, name, req.quantity, req.seat_number)
    recompute_status(req.order_id)

    return {"success": True, "message": f"{name} × {req.quantity} added"}


@app.post("/orders/remove-item")
def remove_item(req: RemoveItemRequest):
    _, err = require_role(req.session_token, {"MANAGER", "SERVER"})
    if err:
        return {"success": False, "message": err}

    if req.order_id not in orders:
        return {"success": False, "message": "Order not found"}
    if req.quantity < 1:
        return {"success": False, "message": "Quantity must be at least 1"}

    order = orders[req.order_id]
    name = normalize_item_name(req.item_name)
    if name is None:
        return {"success": False, "message": "Invalid menu item"}
    if order["status"] == "PAID":
        return {"success": False, "message": "Cannot modify a paid order"}

    if order["type"] == "dine_in":
        if req.seat_number < 1 or req.seat_number > order["guests"]:
            return {"success": False, "message": f"Seat must be 1–{order['guests']}"}
    else:
        if req.seat_number != 0:
            return {"success": False, "message": "Seat must be 0 for take-out"}

    existing = count_qty(order, name, req.seat_number)
    if existing == 0:
        return {"success": False, "message": "Item not in this order"}
    if req.quantity > existing:
        return {"success": False, "message": f"Only {existing} of those exist"}

    remove_lines(order, name, req.quantity, req.seat_number)
    remove_from_kitchen(req.order_id, name, req.quantity, req.seat_number)
    recompute_status(req.order_id)

    return {"success": True, "message": f"{name} × {req.quantity} removed"}


@app.get("/orders")
def list_orders():
    summaries = []
    for oid in sorted(orders.keys(), reverse=True):
        o = orders[oid]
        total = order_total(o)
        paid  = money(o.get("paid", 0.0))
        rem   = total - paid
        if rem < Decimal("0.00"): rem = Decimal("0.00")
        summaries.append({
            "order_id":      oid,
            "type":          o["type"],
            "status":        o["status"],
            "table_number":  o["table_number"],
            "guests":        o["guests"],
            "customer_name": o.get("customer_name", ""),
            "items":         o["items"],
            "total":         round_money(total),
            "paid":          round_money(paid),
            "remaining":     round_money(rem),
        })
    return {"success": True, "orders": summaries}


@app.get("/orders/{order_id}")
def view_order(order_id: int):
    if order_id not in orders:
        return {"success": False, "message": "Order not found"}

    o     = orders[order_id]
    total = order_total(o)
    paid  = money(o.get("paid", 0.0))
    rem   = total - paid
    if rem < Decimal("0.00"): rem = Decimal("0.00")

    seat_totals = []
    if o["type"] == "dine_in":
        for s in range(1, o["guests"] + 1):
            st  = seat_total(o, s)
            sp  = money(o["seat_paid"].get(s, 0.0))
            sr  = st - sp
            if sr < Decimal("0.00"): sr = Decimal("0.00")
            seat_totals.append({
                "seat_number": s,
                "total":       round_money(st),
                "paid":        round_money(sp),
                "remaining":   round_money(sr),
                "fully_paid":  sr == Decimal("0.00") and st > Decimal("0.00"),
            })

    items_with_kitchen = []
    for it in o["items"]:
        sent, kstatus = seat_kitchen_status(order_id, it["seat_number"], it["item_name"])
        items_with_kitchen.append({
            **it,
            "sent_to_kitchen": sent,
            "kitchen_status":  kstatus,
        })

    out = {**o, "items": items_with_kitchen}

    return {
        "success":     True,
        "order":       out,
        "total":       round_money(total),
        "paid":        round_money(paid),
        "remaining":   round_money(rem),
        "seat_totals": seat_totals,
        "fully_paid":  rem == Decimal("0.00") and total > Decimal("0.00"),
    }


@app.post("/orders/mark-ready")
def mark_order_ready(req: MarkOrderReadyRequest):
    _, err = require_role(req.session_token, {"CHEF", "MANAGER"})
    if err:
        return {"success": False, "message": err}
    if req.order_id not in orders:
        return {"success": False, "message": "Order not found"}
    orders[req.order_id]["status"] = "READY"
    return {"success": True, "message": f"Order #{req.order_id} marked ready"}


# ── Kitchen ──────────────────────────────────────────────────────────────────

@app.get("/kitchen")
def view_kitchen(view_type: str = "PENDING_ONLY"):
    """view_type: PENDING_ONLY | ALL_ITEMS"""
    if view_type.upper() == "ALL_ITEMS":
        items = list(kitchen_queue)
    else:
        items = [k for k in kitchen_queue if k["status"] == "PENDING"]
    items.sort(key=lambda x: (x["created_at"], x["kitchen_item_id"]))
    return {"success": True, "items": items}

@app.post("/kitchen/complete")
def complete_kitchen_item(req: CompleteKitchenItemRequest):
    _, err = require_role(req.session_token, {"CHEF", "MANAGER"})
    if err:
        return {"success": False, "message": err}

    target = next((k for k in kitchen_queue if k["kitchen_item_id"] == req.kitchen_item_id), None)
    if target is None:
        return {"success": False, "message": "Kitchen item not found"}
    if target["status"] == "COMPLETED":
        return {"success": False, "message": "Already completed"}

    target["status"] = "COMPLETED"
    if target["order_id"] in orders:
        recompute_status(target["order_id"])
    return {"success": True, "message": f"{target['item_name']} × {target['quantity']} completed"}


# ── Cashout ──────────────────────────────────────────────────────────────────

@app.post("/orders/cashout")
def cashout(req: CashoutRequest):
    _, err = require_role(req.session_token, {"MANAGER", "SERVER"})
    if err:
        return {"success": False, "message": err}

    if req.order_id not in orders:
        return {"success": False, "message": "Order not found"}

    order  = orders[req.order_id]
    total  = order_total(order)
    target = req.payment_target.upper().strip()

    if total == Decimal("0.00"):
        return {"success": False, "message": "Order has no items to charge"}

    amount_charged = Decimal("0.00")

    if order["type"] == "dine_in":
        if target == "SEAT":
            if req.seat_number < 1 or req.seat_number > order["guests"]:
                return {"success": False, "message": "Invalid seat number"}
            st  = seat_total(order, req.seat_number)
            sp  = money(order["seat_paid"].get(req.seat_number, 0.0))
            sr  = st - sp
            if sr <= Decimal("0.00"):
                return {"success": False, "message": "Seat already paid"}
            order["seat_paid"][req.seat_number] = float(st)
            order["paid"] = float(money(order["paid"]) + sr)
            amount_charged = sr

        elif target == "TABLE":
            current_paid = money(order["paid"])
            rem = total - current_paid
            if rem <= Decimal("0.00"):
                return {"success": False, "message": "Table already paid"}
            for s in range(1, order["guests"] + 1):
                order["seat_paid"][s] = float(seat_total(order, s))
            order["paid"] = float(total)
            amount_charged = rem

        else:
            return {"success": False, "message": "For dine-in, payment_target must be SEAT or TABLE"}

    else:  # take_out
        if target != "TAKEOUT_ORDER":
            return {"success": False, "message": "For take-out, payment_target must be TAKEOUT_ORDER"}
        current_paid = money(order["paid"])
        rem = total - current_paid
        if rem <= Decimal("0.00"):
            return {"success": False, "message": "Order already paid"}
        order["paid"] = float(total)
        amount_charged = rem

    recompute_status(req.order_id)

    paid_now  = money(order["paid"])
    remaining = total - paid_now
    if remaining < Decimal("0.00"): remaining = Decimal("0.00")

    return {
        "success":           True,
        "message":           f"Charged ${round_money(amount_charged):.2f}",
        "order_id":          req.order_id,
        "amount_charged":    round_money(amount_charged),
        "total_paid":        round_money(paid_now),
        "remaining_balance": round_money(remaining),
        "status":            order["status"],
    }


# ── Waitlist ─────────────────────────────────────────────────────────────────

@app.get("/waitlist")
def view_waitlist():
    return {"success": True, "entries": waitlist}

@app.post("/waitlist/add")
def add_to_waitlist(req: AddToWaitlistRequest):
    global next_waitlist_id
    _, err = require_role(req.session_token, {"MANAGER", "SERVER"})
    if err:
        return {"success": False, "message": err}
    if not req.customer_name.strip():
        return {"success": False, "message": "Customer name required"}
    if req.party_size < 1 or req.party_size > 6:
        return {"success": False, "message": "Party size must be 1–6"}

    wid = next_waitlist_id
    next_waitlist_id += 1
    waitlist.append({
        "waitlist_id":   wid,
        "customer_name": req.customer_name.strip(),
        "party_size":    req.party_size,
        "timestamp":     now_str(),
    })
    return {"success": True, "message": "Party added to waitlist", "waitlist_id": wid}

@app.post("/waitlist/seat")
def seat_waitlist_party(req: SeatWaitlistPartyRequest):
    global next_order_id
    _, err = require_role(req.session_token, {"MANAGER", "SERVER"})
    if err:
        return {"success": False, "message": err}

    entry = next((w for w in waitlist if w["waitlist_id"] == req.waitlist_id), None)
    if entry is None:
        return {"success": False, "message": "Waitlist entry not found"}

    if req.table_number not in tables:
        return {"success": False, "message": "Table does not exist"}
    if tables[req.table_number]["occupied"]:
        return {"success": False, "message": "Table is occupied"}
    if entry["party_size"] > tables[req.table_number]["capacity"]:
        return {"success": False, "message": "Party doesn't fit at that table"}

    next_order_id += 1
    oid = next_order_id
    orders[oid] = {
        "order_id":      oid,
        "type":          "dine_in",
        "status":        "PLACED",
        "table_number":  req.table_number,
        "guests":        entry["party_size"],
        "customer_name": entry["customer_name"],
        "items":         [],
        "paid":          0.0,
        "seat_paid":     {},
        "created_at":    now_str(),
    }
    occupy_table(req.table_number, oid)
    waitlist.remove(entry)

    return {"success": True, "message": f"{entry['customer_name']} seated at table {req.table_number}", "order_id": oid}
