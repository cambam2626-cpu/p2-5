import grpc
from concurrent import futures
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import restaurant_pb2
import restaurant_pb2_grpc


users = {
    "manager": {"password": "pw", "role": "MANAGER"},
    "server1": {"password": "pw", "role": "SERVER"},
    "server2": {"password": "pw", "role": "SERVER"},
    "chef": {"password": "pw", "role": "CHEF"},
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

menu_by_name = {
    "Fried Pickles": {"category": restaurant_pb2.STARTER, "price": 6.99, "available": True},
    "Wings": {"category": restaurant_pb2.STARTER, "price": 9.99, "available": True},
    "Stuffed Mushrooms": {"category": restaurant_pb2.STARTER, "price": 7.99, "available": True},

    "Cheeseburger": {"category": restaurant_pb2.MAIN, "price": 12.99, "available": True},
    "Chili Cheese Dog": {"category": restaurant_pb2.MAIN, "price": 10.99, "available": True},
    "Cuban Sandwich": {"category": restaurant_pb2.MAIN, "price": 13.99, "available": True},
    "Full Rack Ribs": {"category": restaurant_pb2.MAIN, "price": 18.99, "available": True},
    "Buffalo Chicken Sandwich": {"category": restaurant_pb2.MAIN, "price": 11.99, "available": True},

    "Chocolate Cake": {"category": restaurant_pb2.DESSERT, "price": 6.99, "available": True},
    "Banana Split": {"category": restaurant_pb2.DESSERT, "price": 7.99, "available": True},
    "Honey Ice Cream": {"category": restaurant_pb2.DESSERT, "price": 5.99, "available": True},

    "Coke": {"category": restaurant_pb2.DRINK, "price": 2.99, "available": True},
    "Sprite": {"category": restaurant_pb2.DRINK, "price": 2.99, "available": True},
    "Fanta": {"category": restaurant_pb2.DRINK, "price": 2.99, "available": True},
    "Water": {"category": restaurant_pb2.DRINK, "price": 0.00, "available": True},
    "Milkshake": {"category": restaurant_pb2.DRINK, "price": 4.99, "available": True},
}


def _money(value):
    return Decimal(str(value))


def _round_money(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _get_session(session_token):
    return sessions.get(session_token)


def _require_role(session_token, allowed_roles):
    sess = _get_session(session_token)
    if not sess:
        return None, "Unauthorized"
    if sess["role"] not in allowed_roles:
        return None, "Forbidden"
    return sess, None


def _menu_as_repeated_items():
    items = []
    for name, rec in menu_by_name.items():
        items.append(
            restaurant_pb2.MenuItem(
                name=name,
                category=rec["category"],
                price=float(rec["price"]),
                available=bool(rec["available"]),
            )
        )
    items.sort(key=lambda m: (int(m.category), m.name))
    return items


def _normalize_item_name(item_name):
    clean_name = item_name.strip().lower()
    for name in menu_by_name:
        if name.lower() == clean_name:
            return name
    return None


def _is_kitchen_item(item_name):
    if item_name not in menu_by_name:
        return False
    return menu_by_name[item_name]["category"] != restaurant_pb2.DRINK


def _current_timestamp_string():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _table_exists(table_number):
    return table_number in tables


def _table_available(table_number):
    return _table_exists(table_number) and not tables[table_number]["occupied"]


def _party_fits_table(table_number, guests):
    return _table_exists(table_number) and guests <= tables[table_number]["capacity"]


def _occupy_table(table_number, order_id):
    tables[table_number]["occupied"] = True
    tables[table_number]["order_id"] = order_id


def _release_table(table_number):
    if _table_exists(table_number):
        tables[table_number]["occupied"] = False
        tables[table_number]["order_id"] = None


def _new_order_line(item_name, quantity, seat_number):
    return {
        "item_name": item_name,
        "quantity": quantity,
        "unit_price": float(menu_by_name[item_name]["price"]),
        "seat_number": seat_number,
    }


def _get_order_total_decimal(order):
    total = Decimal("0.00")
    for line in order["lines"]:
        total += _money(line["unit_price"]) * line["quantity"]
    return total


def _get_seat_total_decimal(order, seat_number):
    total = Decimal("0.00")
    for line in order["lines"]:
        if line["seat_number"] == seat_number:
            total += _money(line["unit_price"]) * line["quantity"]
    return total


def _get_takeout_item_count(order):
    count = 0
    for line in order["lines"]:
        count += line["quantity"]
    return count


def _recompute_order_status(order_id):
    order = orders[order_id]
    total = _get_order_total_decimal(order)
    paid = _money(order.get("paid_amount", 0.0))

    kitchen_pending_exists = False
    for item in kitchen_queue:
        if item["order_id"] == order_id and item["status"] == restaurant_pb2.PENDING:
            kitchen_pending_exists = True
            break

    if paid >= total and total > Decimal("0.00"):
        order["status"] = restaurant_pb2.PAID
        if order["type"] == restaurant_pb2.DINE_IN:
            _release_table(order["table_number"])
        return

    if paid > Decimal("0.00"):
        order["status"] = restaurant_pb2.PARTIALLY_PAID
        return

    if kitchen_pending_exists:
        order["status"] = restaurant_pb2.PLACED
    else:
        order["status"] = restaurant_pb2.READY


def _add_to_kitchen_queue(order_id, order, item_name, quantity, seat_number):
    global next_kitchen_item_id

    if not _is_kitchen_item(item_name):
        return

    kitchen_queue.append({
        "kitchen_item_id": next_kitchen_item_id,
        "order_id": order_id,
        "table_number": order["table_number"],
        "seat_number": seat_number,
        "customer_name": order.get("customer_name", ""),
        "item_name": item_name,
        "quantity": quantity,
        "timestamp": _current_timestamp_string(),
        "created_at": time.time(),
        "status": restaurant_pb2.PENDING,
    })
    next_kitchen_item_id += 1


def _remove_from_kitchen_queue(order_id, item_name, quantity, seat_number):
    remaining_to_remove = quantity

    pending_items = sorted(
        [
            item for item in kitchen_queue
            if item["order_id"] == order_id
            and item["item_name"] == item_name
            and item["seat_number"] == seat_number
            and item["status"] == restaurant_pb2.PENDING
        ],
        key=lambda x: (x["created_at"], x["kitchen_item_id"]),
        reverse=True
    )

    for item in pending_items:
        if remaining_to_remove <= 0:
            break

        if item["quantity"] <= remaining_to_remove:
            remaining_to_remove -= item["quantity"]
            item["status"] = restaurant_pb2.COMPLETED
        else:
            item["quantity"] -= remaining_to_remove
            remaining_to_remove = 0


def _remove_order_lines(order, item_name, quantity, seat_number):
    remaining_to_remove = quantity

    for line in reversed(order["lines"]):
        if remaining_to_remove <= 0:
            break

        if line["item_name"] == item_name and line["seat_number"] == seat_number:
            if line["quantity"] <= remaining_to_remove:
                remaining_to_remove -= line["quantity"]
                order["lines"].remove(line)
            else:
                line["quantity"] -= remaining_to_remove
                remaining_to_remove = 0

    return remaining_to_remove == 0


def _count_item_quantity(order, item_name, seat_number):
    total_qty = 0
    for line in order["lines"]:
        if line["item_name"] == item_name and line["seat_number"] == seat_number:
            total_qty += line["quantity"]
    return total_qty


def _seat_kitchen_status(order_id, seat_number, item_name):
    has_pending = False
    has_completed = False

    for item in kitchen_queue:
        if (
            item["order_id"] == order_id
            and item["seat_number"] == seat_number
            and item["item_name"] == item_name
        ):
            if item["status"] == restaurant_pb2.PENDING:
                has_pending = True
            elif item["status"] == restaurant_pb2.COMPLETED:
                has_completed = True

    if has_pending:
        return True, restaurant_pb2.PENDING
    if has_completed:
        return True, restaurant_pb2.COMPLETED
    return False, restaurant_pb2.COMPLETED


class RestaurantService(restaurant_pb2_grpc.RestaurantServiceServicer):

    def Login(self, request, context):
        if request.username in users and users[request.username]["password"] == request.password:
            token = f"{request.username}_session"
            sessions[token] = {
                "username": request.username,
                "role": users[request.username]["role"]
            }
            return restaurant_pb2.LoginResponse(
                success=True,
                message="Login successful",
                session_token=token
            )

        return restaurant_pb2.LoginResponse(
            success=False,
            message="Invalid credentials",
            session_token=""
        )

    def Logout(self, request, context):
        if request.session_token in sessions:
            del sessions[request.session_token]
            return restaurant_pb2.LogoutResponse(success=True, message="Logged out")

        return restaurant_pb2.LogoutResponse(success=False, message="Invalid session token")

    def GetMenu(self, request, context):
        if request.session_token not in sessions:
            return restaurant_pb2.MenuResponse(success=False, message="Unauthorized", items=[])

        return restaurant_pb2.MenuResponse(
            success=True,
            message="Menu retrieved",
            items=_menu_as_repeated_items()
        )

    def UpdatePrice(self, request, context):
        _, err = _require_role(request.session_token, {"MANAGER"})
        if err:
            return restaurant_pb2.UpdatePriceResponse(success=False, message=err)

        canonical_name = _normalize_item_name(request.item_name)
        if canonical_name is None:
            return restaurant_pb2.UpdatePriceResponse(success=False, message="Invalid menu item")

        if request.new_price < 0:
            return restaurant_pb2.UpdatePriceResponse(success=False, message="Invalid price")

        menu_by_name[canonical_name]["price"] = float(request.new_price)
        return restaurant_pb2.UpdatePriceResponse(success=True, message="Price updated")

    def CreateOrder(self, request, context):
        global next_order_id

        _, err = _require_role(request.session_token, {"MANAGER", "SERVER"})
        if err:
            return restaurant_pb2.CreateOrderResponse(success=False, message=err, order_id=0)

        if request.type == restaurant_pb2.DINE_IN:
            if request.guests < 1 or request.guests > 6:
                return restaurant_pb2.CreateOrderResponse(
                    success=False,
                    message="Invalid guest count",
                    order_id=0
                )

            if not _table_exists(request.table_number):
                return restaurant_pb2.CreateOrderResponse(
                    success=False,
                    message="Table does not exist",
                    order_id=0
                )

            if not _party_fits_table(request.table_number, request.guests):
                return restaurant_pb2.CreateOrderResponse(
                    success=False,
                    message="Party does not fit at this table",
                    order_id=0
                )

            if not _table_available(request.table_number):
                return restaurant_pb2.CreateOrderResponse(
                    success=False,
                    message="Table is currently occupied",
                    order_id=0
                )

        elif request.type == restaurant_pb2.TAKE_OUT:
            if not request.customer_name.strip():
                return restaurant_pb2.CreateOrderResponse(
                    success=False,
                    message="Take-out orders require customer name",
                    order_id=0
                )
        else:
            return restaurant_pb2.CreateOrderResponse(
                success=False,
                message="Invalid order type",
                order_id=0
            )

        next_order_id += 1
        order_id = next_order_id

        orders[order_id] = {
            "type": request.type,
            "status": restaurant_pb2.PLACED,
            "table_number": request.table_number if request.type == restaurant_pb2.DINE_IN else 0,
            "guests": request.guests if request.type == restaurant_pb2.DINE_IN else 0,
            "customer_name": request.customer_name.strip() if request.type == restaurant_pb2.TAKE_OUT else "",
            "lines": [],
            "paid_amount": 0.0,
            "seat_paid": {},
        }

        if request.type == restaurant_pb2.DINE_IN:
            _occupy_table(request.table_number, order_id)

        return restaurant_pb2.CreateOrderResponse(
            success=True,
            message="Order created",
            order_id=order_id
        )

    def AddItem(self, request, context):
        _, err = _require_role(request.session_token, {"MANAGER", "SERVER"})
        if err:
            return restaurant_pb2.OrderResponse(success=False, message=err)

        if request.order_id not in orders:
            return restaurant_pb2.OrderResponse(success=False, message="Invalid order identifier")

        if request.quantity < 1:
            return restaurant_pb2.OrderResponse(success=False, message="Invalid quantity")

        order = orders[request.order_id]
        canonical_name = _normalize_item_name(request.item_name)

        if canonical_name is None:
            return restaurant_pb2.OrderResponse(success=False, message="Invalid menu item")

        if not menu_by_name[canonical_name]["available"]:
            return restaurant_pb2.OrderResponse(success=False, message="Item unavailable")

        if order["status"] == restaurant_pb2.PAID:
            return restaurant_pb2.OrderResponse(success=False, message="Cannot modify a paid order")

        if order["type"] == restaurant_pb2.DINE_IN:
            if request.seat_number < 1 or request.seat_number > order["guests"]:
                return restaurant_pb2.OrderResponse(success=False, message="Invalid seat number")

            order["lines"].append(_new_order_line(canonical_name, request.quantity, request.seat_number))

            _add_to_kitchen_queue(request.order_id, order, canonical_name, request.quantity, request.seat_number)
            _recompute_order_status(request.order_id)

            return restaurant_pb2.OrderResponse(success=True, message="Item added")

        if request.seat_number != 0:
            return restaurant_pb2.OrderResponse(success=False, message="Seat number must be 0 for take-out")

        if _get_takeout_item_count(order) + request.quantity > 10:
            return restaurant_pb2.OrderResponse(success=False, message="Take-out item limit exceeded (max 10)")

        order["lines"].append(_new_order_line(canonical_name, request.quantity, 0))

        _add_to_kitchen_queue(request.order_id, order, canonical_name, request.quantity, 0)
        _recompute_order_status(request.order_id)

        return restaurant_pb2.OrderResponse(success=True, message="Item added")

    def RemoveItem(self, request, context):
        _, err = _require_role(request.session_token, {"MANAGER", "SERVER"})
        if err:
            return restaurant_pb2.OrderResponse(success=False, message=err)

        if request.order_id not in orders:
            return restaurant_pb2.OrderResponse(success=False, message="Invalid order identifier")

        if request.quantity < 1:
            return restaurant_pb2.OrderResponse(success=False, message="Invalid quantity")

        order = orders[request.order_id]
        canonical_name = _normalize_item_name(request.item_name)

        if canonical_name is None:
            return restaurant_pb2.OrderResponse(success=False, message="Invalid menu item")

        if order["status"] == restaurant_pb2.PAID:
            return restaurant_pb2.OrderResponse(success=False, message="Cannot modify a paid order")

        if order["type"] == restaurant_pb2.DINE_IN:
            if request.seat_number < 1 or request.seat_number > order["guests"]:
                return restaurant_pb2.OrderResponse(success=False, message="Invalid seat number")

            existing_qty = _count_item_quantity(order, canonical_name, request.seat_number)
            if existing_qty == 0:
                return restaurant_pb2.OrderResponse(success=False, message="Item not in order")

            if request.quantity > existing_qty:
                return restaurant_pb2.OrderResponse(success=False, message="Remove quantity exceeds existing quantity")

            _remove_order_lines(order, canonical_name, request.quantity, request.seat_number)
            _remove_from_kitchen_queue(request.order_id, canonical_name, request.quantity, request.seat_number)
            _recompute_order_status(request.order_id)

            return restaurant_pb2.OrderResponse(success=True, message="Item removed")

        if request.seat_number != 0:
            return restaurant_pb2.OrderResponse(success=False, message="Seat number must be 0 for take-out")

        existing_qty = _count_item_quantity(order, canonical_name, 0)
        if existing_qty == 0:
            return restaurant_pb2.OrderResponse(success=False, message="Item not in order")

        if request.quantity > existing_qty:
            return restaurant_pb2.OrderResponse(success=False, message="Remove quantity exceeds existing quantity")

        _remove_order_lines(order, canonical_name, request.quantity, 0)
        _remove_from_kitchen_queue(request.order_id, canonical_name, request.quantity, 0)
        _recompute_order_status(request.order_id)

        return restaurant_pb2.OrderResponse(success=True, message="Item removed")

    def GetBill(self, request, context):
        if request.session_token not in sessions:
            return restaurant_pb2.BillResponse(success=False, message="Unauthorized")

        if request.order_id not in orders:
            return restaurant_pb2.BillResponse(success=False, message="Invalid order identifier")

        order = orders[request.order_id]
        total = _get_order_total_decimal(order)
        paid = _money(order.get("paid_amount", 0.0))
        remaining = total - paid
        if remaining < Decimal("0.00"):
            remaining = Decimal("0.00")

        seat_totals = []

        if order["type"] == restaurant_pb2.DINE_IN:
            for seat_number in range(1, order["guests"] + 1):
                seat_total = _get_seat_total_decimal(order, seat_number)
                seat_paid = _money(order["seat_paid"].get(seat_number, 0.0))
                seat_remaining = seat_total - seat_paid
                if seat_remaining < Decimal("0.00"):
                    seat_remaining = Decimal("0.00")

                seat_totals.append(
                    restaurant_pb2.SeatTotal(
                        seat_number=seat_number,
                        total=_round_money(seat_total),
                        paid=_round_money(seat_paid),
                        remaining=_round_money(seat_remaining),
                        fully_paid=(seat_remaining == Decimal("0.00"))
                    )
                )

        return restaurant_pb2.BillResponse(
            success=True,
            message="Bill calculated",
            order_id=request.order_id,
            type=order["type"],
            table_number=order["table_number"],
            customer_name=order.get("customer_name", ""),
            total=_round_money(total),
            paid=_round_money(paid),
            remaining=_round_money(remaining),
            seat_totals=seat_totals,
            fully_paid=(remaining == Decimal("0.00"))
        )

    def ListOrders(self, request, context):
        _, err = _require_role(request.session_token, {"MANAGER", "SERVER", "CHEF"})
        if err:
            return restaurant_pb2.ListOrdersResponse(success=False, message=err, orders=[])

        summaries = []
        for order_id, order in sorted(orders.items()):
            total = _get_order_total_decimal(order)
            paid = _money(order.get("paid_amount", 0.0))
            remaining = total - paid
            if remaining < Decimal("0.00"):
                remaining = Decimal("0.00")

            summaries.append(
                restaurant_pb2.OrderSummary(
                    order_id=order_id,
                    type=order["type"],
                    status=order["status"],
                    table_number=order["table_number"],
                    guests=order["guests"],
                    customer_name=order.get("customer_name", ""),
                    total=_round_money(total),
                    paid=_round_money(paid),
                    remaining=_round_money(remaining)
                )
            )

        return restaurant_pb2.ListOrdersResponse(success=True, message="Orders listed", orders=summaries)

    def MarkOrderReady(self, request, context):
        _, err = _require_role(request.session_token, {"CHEF"})
        if err:
            return restaurant_pb2.MarkOrderReadyResponse(success=False, message=err)

        if request.order_id not in orders:
            return restaurant_pb2.MarkOrderReadyResponse(success=False, message="Invalid order identifier")

        orders[request.order_id]["status"] = restaurant_pb2.READY
        return restaurant_pb2.MarkOrderReadyResponse(success=True, message="Order marked ready")

    def ViewKitchenQueue(self, request, context):
        _, err = _require_role(request.session_token, {"CHEF", "MANAGER"})
        if err:
            return restaurant_pb2.ViewKitchenQueueResponse(success=False, message=err, items=[])

        if request.view_type == restaurant_pb2.PENDING_ONLY:
            items = [item for item in kitchen_queue if item["status"] == restaurant_pb2.PENDING]
        else:
            items = list(kitchen_queue)

        items.sort(key=lambda item: (item["created_at"], item["kitchen_item_id"]))

        response_items = []
        for item in items:
            response_items.append(
                restaurant_pb2.KitchenItem(
                    kitchen_item_id=item["kitchen_item_id"],
                    order_id=item["order_id"],
                    table_number=item["table_number"],
                    seat_number=item["seat_number"],
                    customer_name=item.get("customer_name", ""),
                    item_name=item["item_name"],
                    quantity=item["quantity"],
                    timestamp=item["timestamp"],
                    status=item["status"]
                )
            )

        return restaurant_pb2.ViewKitchenQueueResponse(
            success=True,
            message="Kitchen queue listed",
            items=response_items
        )

    def CompleteKitchenItem(self, request, context):
        _, err = _require_role(request.session_token, {"CHEF"})
        if err:
            return restaurant_pb2.CompleteKitchenItemResponse(success=False, message=err)

        target_item = None
        for item in kitchen_queue:
            if item["kitchen_item_id"] == request.kitchen_item_id:
                target_item = item
                break

        if target_item is None:
            return restaurant_pb2.CompleteKitchenItemResponse(
                success=False,
                message="Invalid kitchen item identifier"
            )

        if target_item["status"] == restaurant_pb2.COMPLETED:
            return restaurant_pb2.CompleteKitchenItemResponse(
                success=False,
                message="Kitchen item already completed"
            )

        target_item["status"] = restaurant_pb2.COMPLETED

        order_id = target_item["order_id"]
        if order_id in orders:
            _recompute_order_status(order_id)

        return restaurant_pb2.CompleteKitchenItemResponse(
            success=True,
            message="Kitchen item completed"
        )

    def ViewOrder(self, request, context):
        _, err = _require_role(request.session_token, {"MANAGER", "SERVER", "CHEF"})
        if err:
            return restaurant_pb2.ViewOrderResponse(success=False, message=err)

        if request.order_id not in orders:
            return restaurant_pb2.ViewOrderResponse(success=False, message="Invalid order identifier")

        order = orders[request.order_id]
        total = _get_order_total_decimal(order)
        paid = _money(order.get("paid_amount", 0.0))
        remaining = total - paid
        if remaining < Decimal("0.00"):
            remaining = Decimal("0.00")

        seat_totals = []
        detail_items = []

        if order["type"] == restaurant_pb2.DINE_IN:
            for seat_number in range(1, order["guests"] + 1):
                seat_total = _get_seat_total_decimal(order, seat_number)
                seat_paid = _money(order["seat_paid"].get(seat_number, 0.0))
                seat_remaining = seat_total - seat_paid
                if seat_remaining < Decimal("0.00"):
                    seat_remaining = Decimal("0.00")

                seat_totals.append(
                    restaurant_pb2.SeatTotal(
                        seat_number=seat_number,
                        total=_round_money(seat_total),
                        paid=_round_money(seat_paid),
                        remaining=_round_money(seat_remaining),
                        fully_paid=(seat_remaining == Decimal("0.00"))
                    )
                )

        for line in order["lines"]:
            sent_to_kitchen, kitchen_status = _seat_kitchen_status(
                request.order_id,
                line["seat_number"],
                line["item_name"]
            )

            unit_price = _money(line["unit_price"])
            detail_items.append(
                restaurant_pb2.OrderItemDetail(
                    item_name=line["item_name"],
                    quantity=line["quantity"],
                    unit_price=_round_money(unit_price),
                    line_total=_round_money(unit_price * line["quantity"]),
                    seat_number=line["seat_number"],
                    sent_to_kitchen=sent_to_kitchen,
                    kitchen_status=kitchen_status
                )
            )

        return restaurant_pb2.ViewOrderResponse(
            success=True,
            message="Order viewed",
            order_id=request.order_id,
            type=order["type"],
            status=order["status"],
            table_number=order["table_number"],
            guests=order["guests"],
            customer_name=order.get("customer_name", ""),
            total=_round_money(total),
            paid=_round_money(paid),
            remaining=_round_money(remaining),
            seat_totals=seat_totals,
            items=detail_items
        )

    def ListTables(self, request, context):
        _, err = _require_role(request.session_token, {"MANAGER", "SERVER", "CHEF"})
        if err:
            return restaurant_pb2.ListTablesResponse(success=False, message=err, tables=[])

        table_list = []
        for table_number, table in sorted(tables.items()):
            order_id = table["order_id"] if table["order_id"] is not None else 0
            table_list.append(
                restaurant_pb2.TableInfo(
                    table_number=table_number,
                    capacity=table["capacity"],
                    occupied=table["occupied"],
                    order_id=order_id
                )
            )

        return restaurant_pb2.ListTablesResponse(
            success=True,
            message="Tables listed",
            tables=table_list
        )

    def AddToWaitlist(self, request, context):
        global next_waitlist_id

        _, err = _require_role(request.session_token, {"MANAGER", "SERVER"})
        if err:
            return restaurant_pb2.AddToWaitlistResponse(success=False, message=err, waitlist_id=0)

        if not request.customer_name.strip():
            return restaurant_pb2.AddToWaitlistResponse(
                success=False,
                message="Customer name required",
                waitlist_id=0
            )

        if request.party_size < 1 or request.party_size > 6:
            return restaurant_pb2.AddToWaitlistResponse(
                success=False,
                message="Invalid party size",
                waitlist_id=0
            )

        waitlist_id = next_waitlist_id
        next_waitlist_id += 1

        waitlist.append({
            "waitlist_id": waitlist_id,
            "customer_name": request.customer_name.strip(),
            "party_size": request.party_size,
            "timestamp": _current_timestamp_string(),
        })

        return restaurant_pb2.AddToWaitlistResponse(
            success=True,
            message="Party added to waitlist",
            waitlist_id=waitlist_id
        )

    def ViewWaitlist(self, request, context):
        _, err = _require_role(request.session_token, {"MANAGER", "SERVER"})
        if err:
            return restaurant_pb2.ViewWaitlistResponse(success=False, message=err, entries=[])

        entries = []
        for entry in waitlist:
            entries.append(
                restaurant_pb2.WaitlistEntry(
                    waitlist_id=entry["waitlist_id"],
                    customer_name=entry["customer_name"],
                    party_size=entry["party_size"],
                    timestamp=entry["timestamp"]
                )
            )

        return restaurant_pb2.ViewWaitlistResponse(
            success=True,
            message="Waitlist listed",
            entries=entries
        )

    def SeatWaitlistParty(self, request, context):
        global next_order_id

        _, err = _require_role(request.session_token, {"MANAGER", "SERVER"})
        if err:
            return restaurant_pb2.SeatWaitlistPartyResponse(success=False, message=err, order_id=0)

        target_entry = None
        for entry in waitlist:
            if entry["waitlist_id"] == request.waitlist_id:
                target_entry = entry
                break

        if target_entry is None:
            return restaurant_pb2.SeatWaitlistPartyResponse(
                success=False,
                message="Invalid waitlist ID",
                order_id=0
            )

        if not _table_exists(request.table_number):
            return restaurant_pb2.SeatWaitlistPartyResponse(
                success=False,
                message="Table does not exist",
                order_id=0
            )

        if not _party_fits_table(request.table_number, target_entry["party_size"]):
            return restaurant_pb2.SeatWaitlistPartyResponse(
                success=False,
                message="Party does not fit at this table",
                order_id=0
            )

        if not _table_available(request.table_number):
            return restaurant_pb2.SeatWaitlistPartyResponse(
                success=False,
                message="Table is currently occupied",
                order_id=0
            )

        next_order_id += 1
        order_id = next_order_id

        orders[order_id] = {
            "type": restaurant_pb2.DINE_IN,
            "status": restaurant_pb2.PLACED,
            "table_number": request.table_number,
            "guests": target_entry["party_size"],
            "customer_name": target_entry["customer_name"],
            "lines": [],
            "paid_amount": 0.0,
            "seat_paid": {},
        }

        _occupy_table(request.table_number, order_id)
        waitlist.remove(target_entry)

        return restaurant_pb2.SeatWaitlistPartyResponse(
            success=True,
            message="Waitlist party seated and order created",
            order_id=order_id
        )

    def Cashout(self, request, context):
        _, err = _require_role(request.session_token, {"MANAGER", "SERVER"})
        if err:
            return restaurant_pb2.CashoutResponse(success=False, message=err)

        if request.order_id not in orders:
            return restaurant_pb2.CashoutResponse(success=False, message="Invalid order identifier")

        order = orders[request.order_id]
        total = _get_order_total_decimal(order)
        amount_charged = Decimal("0.00")

        if order["type"] == restaurant_pb2.DINE_IN:
            if request.payment_target == restaurant_pb2.SEAT:
                if request.seat_number < 1 or request.seat_number > order["guests"]:
                    return restaurant_pb2.CashoutResponse(success=False, message="Invalid seat number")

                seat_total = _get_seat_total_decimal(order, request.seat_number)
                seat_paid = _money(order["seat_paid"].get(request.seat_number, 0.0))
                seat_remaining = seat_total - seat_paid
                if seat_remaining < Decimal("0.00"):
                    seat_remaining = Decimal("0.00")

                if seat_remaining == Decimal("0.00"):
                    return restaurant_pb2.CashoutResponse(success=False, message="Seat already paid")

                order["seat_paid"][request.seat_number] = float(seat_total)
                order["paid_amount"] = float(_money(order["paid_amount"]) + seat_remaining)
                amount_charged = seat_remaining

            elif request.payment_target == restaurant_pb2.TABLE:
                current_paid = _money(order["paid_amount"])
                remaining = total - current_paid
                if remaining < Decimal("0.00"):
                    remaining = Decimal("0.00")

                if remaining == Decimal("0.00"):
                    return restaurant_pb2.CashoutResponse(success=False, message="Table already fully paid")

                for seat_number in range(1, order["guests"] + 1):
                    seat_total = _get_seat_total_decimal(order, seat_number)
                    order["seat_paid"][seat_number] = float(seat_total)

                order["paid_amount"] = float(total)
                amount_charged = remaining

            else:
                return restaurant_pb2.CashoutResponse(success=False, message="Invalid payment target for dine-in")

        else:
            if request.payment_target != restaurant_pb2.TAKEOUT_ORDER:
                return restaurant_pb2.CashoutResponse(success=False, message="Invalid payment target for take-out")

            current_paid = _money(order["paid_amount"])
            remaining = total - current_paid
            if remaining < Decimal("0.00"):
                remaining = Decimal("0.00")

            if remaining == Decimal("0.00"):
                return restaurant_pb2.CashoutResponse(success=False, message="Take-out order already fully paid")

            order["paid_amount"] = float(total)
            amount_charged = remaining

        _recompute_order_status(request.order_id)

        total_paid = _money(order["paid_amount"])
        remaining_balance = total - total_paid
        if remaining_balance < Decimal("0.00"):
            remaining_balance = Decimal("0.00")

        return restaurant_pb2.CashoutResponse(
            success=True,
            message="Payment processed",
            order_id=request.order_id,
            amount_charged=_round_money(amount_charged),
            total_paid=_round_money(total_paid),
            remaining_balance=_round_money(remaining_balance),
            status=order["status"]
        )


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    restaurant_pb2_grpc.add_RestaurantServiceServicer_to_server(
        RestaurantService(),
        server
    )
    server.add_insecure_port("[::]:50051")
    server.start()

    print("Server started on port 50051")
    print("Restaurant tables loaded:")
    for table_number, table in tables.items():
        print(
            f"  Table {table_number}: capacity {table['capacity']}, "
            f"occupied={table['occupied']}"
        )

    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        server.stop(0)
        print("Server stopped")


if __name__ == "__main__":
    serve()
