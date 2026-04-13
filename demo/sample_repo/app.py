"""
demo/sample_repo/app.py
-----------------------
A deliberately flawed Python module used to demonstrate
the Code Review Agent's detection capabilities.

Contains: SQL injection, mutable default, bare except, hardcoded secret.
"""

import sqlite3
import requests

# ❌ BUG: Hardcoded API key
SECRET_KEY = "sk-prod-demo-a1b2c3d4e5f6"


def get_user(user_id: str, conn: sqlite3.Connection):
    """Fetch user from database by ID."""
    cursor = conn.cursor()
    # ❌ BUG: SQL injection — user_id is interpolated directly
    query = f"SELECT * FROM users WHERE id = '{user_id}'"
    cursor.execute(query)
    return cursor.fetchone()


def add_to_cart(item: str, cart=[]):
    """Add an item to the shopping cart."""
    # ❌ BUG: Mutable default argument — cart is shared across all calls
    cart.append(item)
    return cart


def fetch_price(product_id: str) -> float:
    """Fetch product price from external API."""
    try:
        response = requests.get(
            f"https://api.example.com/products/{product_id}",
            headers={"Authorization": f"Bearer {SECRET_KEY}"},
            timeout=5,
        )
        return response.json()["price"]
    except:
        # ❌ BUG: Bare except swallows KeyboardInterrupt, network errors,
        #         and everything else — caller gets no signal on failure
        return 0.0


def find_similar_products(products: list, target: str) -> list:
    """Find products with matching names — O(n²) implementation."""
    matches = []
    # ❌ BUG: Nested loop is O(n²) — use a set or filter() for O(n)
    for i in range(len(products)):
        for j in range(len(products)):
            if i != j and products[i] == target:
                if products[i] not in matches:
                    matches.append(products[i])
    return matches
