
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
from functools import wraps
import sqlite3
import os
import time

app = Flask(__name__)
app.secret_key = os.environ.get("POS_SECRET_KEY", "dev-only-change-this")
DB_PATH = os.path.join(os.path.dirname(__file__), "pos.db")

REQUESTS = Counter("pos_http_requests_total", "Total POS HTTP requests", ["method", "endpoint"])
ORDERS = Counter("pos_orders_created_total", "Total POS orders created")
START_TIME = time.time()


@app.before_request
def count_request():
    if request.path != "/metrics":
        REQUESTS.labels(request.method, request.endpoint or request.path).inc()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Khác',
                price INTEGER NOT NULL CHECK(price >= 0),
                stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                price INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(id)
            )
        """)
        count = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if count == 0:
            samples = [
                ("Cà phê đen", "Đồ uống", 25000, 50),
                ("Cà phê sữa", "Đồ uống", 30000, 45),
                ("Trà đào", "Đồ uống", 35000, 30),
                ("Trà sữa", "Đồ uống", 40000, 25),
                ("Bánh mì", "Đồ ăn", 20000, 40),
                ("Bánh ngọt", "Đồ ăn", 28000, 20),
                ("Nước suối", "Đồ uống", 10000, 100),
                ("Sinh tố xoài", "Đồ uống", 45000, 15),
            ]
            db.executemany(
                "INSERT INTO products(name, category, price, stock) VALUES (?, ?, ?, ?)",
                samples
            )


@app.get("/")
def index():
    with get_db() as db:
        products = db.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        orders = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        revenue = db.execute("SELECT COALESCE(SUM(total), 0) FROM orders").fetchone()[0]
        low_stock = db.execute("SELECT COUNT(*) FROM products WHERE stock < 10").fetchone()[0]
    return render_template(
        "index.html",
        products=products,
        orders=orders,
        revenue=revenue,
        low_stock=low_stock
    )


@app.post("/product/add")
def add_product():
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "Khác").strip() or "Khác"
    try:
        price = int(request.form.get("price", "0"))
        stock = int(request.form.get("stock", "0"))
    except ValueError:
        return "Giá và số lượng phải là số nguyên.", 400

    if not name or price < 0 or stock < 0:
        return "Thông tin sản phẩm không hợp lệ.", 400

    with get_db() as db:
        db.execute(
            "INSERT INTO products(name, category, price, stock) VALUES (?, ?, ?, ?)",
            (name, category, price, stock)
        )
    return redirect(url_for("index"))


@app.post("/checkout")
def checkout():
    cart = session.get("cart", {})
    if not cart:
        return jsonify({"error": "Giỏ hàng đang trống"}), 400

    with get_db() as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            total = 0
            lines = []

            for product_id, quantity in cart.items():
                product = db.execute(
                    "SELECT * FROM products WHERE id = ?", (int(product_id),)
                ).fetchone()
                quantity = int(quantity)

                if product is None or quantity < 1 or product["stock"] < quantity:
                    raise ValueError("Sản phẩm không tồn tại hoặc không đủ tồn kho.")

                total += product["price"] * quantity
                lines.append((product, quantity))

            cursor = db.execute("INSERT INTO orders(total) VALUES (?)", (total,))
            order_id = cursor.lastrowid

            for product, quantity in lines:
                db.execute(
                    "INSERT INTO order_items(order_id, product_id, product_name, price, quantity) VALUES (?, ?, ?, ?, ?)",
                    (order_id, product["id"], product["name"], product["price"], quantity)
                )
                db.execute(
                    "UPDATE products SET stock = stock - ? WHERE id = ?",
                    (quantity, product["id"])
                )

            db.commit()
        except ValueError as error:
            db.rollback()
            return jsonify({"error": str(error)}), 400

    session["cart"] = {}
    ORDERS.inc()
    return jsonify({"message": "Thanh toán thành công", "order_id": order_id, "total": total})


@app.get("/api/products")
def api_products():
    with get_db() as db:
        products = db.execute("SELECT * FROM products ORDER BY id").fetchall()
    return jsonify([dict(p) for p in products])


@app.get("/api/stats")
def api_stats():
    with get_db() as db:
        orders = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        revenue = db.execute("SELECT COALESCE(SUM(total), 0) FROM orders").fetchone()[0]
        products = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    return jsonify({
        "orders": orders,
        "revenue": revenue,
        "products": products,
        "uptime_seconds": int(time.time() - START_TIME)
    })


@app.get("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
