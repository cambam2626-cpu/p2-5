from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from decimal import Decimal, ROUND_HALF_UP

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

users = {
    "manager": {"password": "pw", "role": "MANAGER"},
    "server1": {"password": "pw", "role": "SERVER"},
    "server2": {"password": "pw", "role": "SERVER"},
    "chef": {"password": "pw", "role": "CHEF"},
}

sessions = {}
orders = {}
next_order_id = 1000

tables = {
    1: {"capacity": 2, "occupied": False, "order_id": None},
    2: {"capacity": 2, "occupied": False, "order_id": None},
    3: {"capacity": 4, "occupied": False, "order_id": None},
    4: {"capacity": 4, "occupied": False, "order_id": None},
    5: {"capacity": 6, "occupied": False, "order_id": None},
}

menu = {
    "Cheeseburger": {"category": "MAIN", "price": 12.99, "available": True},
    "Wings": {"category": "STARTER", "price": 9.99, "available": True},
    "Fried Pickles": {"category": "STARTER", "price": 6.99, "available": True},
    "Cuban Sandwich": {"category": "MAIN", "price": 13.99, "available": True},
    "Chocolate Cake": {"category": "DESSERT", "price": 6.99, "available": True},
    "Coke": {"category": "DRINK", "price": 2.99, "available": True},
    "Water": {"category": "DRINK", "price": 0.00, "available": True},
}


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateOrderRequest(BaseModel):
    session_token: str
    order_type: str
    table_number: int = 0
    guests: int = 0
    customer_name: str = ""


class AddItemRequest(BaseModel):
    session_token: str
    order_id: int
    item_name: str
    quantity: int
    seat_number: int = 0


class UpdatePriceRequest(BaseModel):
    session_token: str
    item_name: str
    new_price: float


def money(value):
    return Decimal(str(value))


def round_money(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize_item_name(item_name):
    for name in menu:
        if name.lower() == item_name.strip().lower():
            return name
    return None


def order_total(order):
    total = Decimal("0.00")
    for item in order["items"]:
        total += money(item["unit_price"]) * item["quantity"]
    return total


def seat_total(order, seat_number):
    total = Decimal("0.00")
    for item in order["items"]:
        if item["seat_number"] == seat_number:
            total += money(item["unit_price"]) * item["quantity"]
    return total


def require_session(token):
    return token in sessions


@app.get("/")
def home():
    return {"message": "Restaurant backend is running"}


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
    }


@app.get("/menu")
def get_menu():
    return {"success": True, "menu": menu}


@app.post("/menu/update-price")
def update_price(req: UpdatePriceRequest):
    if not require_session(req.session_token):
        return {"success": False, "message": "Unauthorized"}

    user = sessions[req.session_token]
    if user["role"] != "MANAGER":
        return {"success": False, "message": "Only managers can update prices"}

    item_name = normalize_item_name(req.item_name)
    if item_name is None:
        return {"success": False, "message": "Invalid menu item"}

    if req.new_price < 0:
        return {"success": False, "message": "Invalid price"}

    menu[item_name]["price"] = req.new_price

    return {"success": True, "message": "Price updated"}


@app.get("/tables")
def get_tables():
    return {"success": True, "tables": tables}


@app.post("/orders")
def create_order(req: CreateOrderRequest):
    global next_order_id

    if not require_session(req.session_token):
        return {"success": False, "message": "Unauthorized"}

    clean_type = req.order_type.strip().lower().replace(" ", "_")

    if clean_type not in ["dine_in", "take_out"]:
        return {"success": False, "message": "Invalid order type"}

    if clean_type == "dine_in":
        if req.table_number not in tables:
            return {"success": False, "message": "Table does not exist"}

        if tables[req.table_number]["occupied"]:
            return {"success": False, "message": "Table is occupied"}

        if req.guests < 1 or req.guests > tables[req.table_number]["capacity"]:
            return {"success": False, "message": "Invalid guest count"}

    if clean_type == "take_out":
        if not req.customer_name.strip():
            return {"success": False, "message": "Take-out orders require customer name"}

    next_order_id += 1
    order_id = next_order_id

    orders[order_id] = {
        "order_id": order_id,
        "type": clean_type,
        "table_number": req.table_number if clean_type == "dine_in" else 0,
        "guests": req.guests if clean_type == "dine_in" else 0,
        "customer_name": req.customer_name.strip() if clean_type == "take_out" else "",
        "items": [],
        "paid": 0.0,
        "seat_paid": {},
    }

    if clean_type == "dine_in":
        tables[req.table_number]["occupied"] = True
        tables[req.table_number]["order_id"] = order_id

    return {"success": True, "message": "Order created", "order_id": order_id}


@app.post("/orders/add-item")
def add_item(req: AddItemRequest):
    if not require_session(req.session_token):
        return {"success": False, "message": "Unauthorized"}

    if req.order_id not in orders:
        return {"success": False, "message": "Invalid order ID"}

    if req.quantity < 1:
        return {"success": False, "message": "Invalid quantity"}

    order = orders[req.order_id]
    item_name = normalize_item_name(req.item_name)

    if item_name is None:
        return {"success": False, "message": "Invalid menu item"}

    if not menu[item_name]["available"]:
        return {"success": False, "message": "Item unavailable"}

    if order["type"] == "dine_in":
        if req.seat_number < 1 or req.seat_number > order["guests"]:
            return {"success": False, "message": "Invalid seat number"}

    if order["type"] == "take_out":
        if req.seat_number != 0:
            return {"success": False, "message": "Seat must be 0 for take-out"}

    order["items"].append({
        "item_name": item_name,
        "quantity": req.quantity,
        "unit_price": menu[item_name]["price"],
        "seat_number": req.seat_number,
        "line_total": round_money(money(menu[item_name]["price"]) * req.quantity),
    })

    return {"success": True, "message": "Item added"}


@app.get("/orders/{order_id}")
def view_order(order_id: int):
    if order_id not in orders:
        return {"success": False, "message": "Invalid order ID"}

    order = orders[order_id]
    total = order_total(order)
    paid = money(order["paid"])
    remaining = total - paid

    seat_totals = []

    if order["type"] == "dine_in":
        for seat in range(1, order["guests"] + 1):
            stotal = seat_total(order, seat)
            spaid = money(order["seat_paid"].get(seat, 0.0))
            sremaining = stotal - spaid

            seat_totals.append({
                "seat_number": seat,
                "total": round_money(stotal),
                "paid": round_money(spaid),
                "remaining": round_money(sremaining),
                "fully_paid": sremaining == Decimal("0.00"),
            })

    return {
        "success": True,
        "order": order,
        "total": round_money(total),
        "paid": round_money(paid),
        "remaining": round_money(remaining),
        "seat_totals": seat_totals,
    }


@app.get("/orders")
def list_orders():
    return {"success": True, "orders": list(orders.values())}
