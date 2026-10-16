import json
import os
import re
import time

import pandas as pd
import requests
from flask import Blueprint, jsonify, request

bp = Blueprint("ledger", __name__)

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("AIPIPE_TOKEN", "")
BASE = os.environ.get("LLM_BASE", "https://aipipe.org/openai/v1")
MODEL = os.environ.get("LLM_MODEL", "gpt-4.1-mini")
TZ = "Asia/Kolkata"


# ---------------------------------------------------------------- data
def load_data():
    with open(os.path.join(HERE, "export.json")) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    with open(os.path.join(HERE, "rates.json")) as f:
        rates = json.load(f)["usd_per_unit"]

    df = pd.DataFrame(rows)
    # parse to real datetimes (mixed 'Z' and '+05:30'), never compare as strings
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True, format="ISO8601")
    df["updated_at"] = pd.to_datetime(df["updated_at"], utc=True, format="ISO8601")

    # same id appears many times -> keep the row with the latest updated_at
    df = df.sort_values(["id", "updated_at"]).drop_duplicates("id", keep="last")

    df["usd"] = (df["amount"] * df["currency"].map(rates)).astype(float)

    # business dates are in Asia/Kolkata
    created = df["created_at"].dt.tz_convert(TZ)
    updated = df["updated_at"].dt.tz_convert(TZ)
    df["created_at"] = created
    df["updated_at"] = updated
    df["month"] = created.dt.strftime("%Y-%m")
    df["date"] = created.dt.strftime("%Y-%m-%d")
    df["updated_month"] = updated.dt.strftime("%Y-%m")
    df["updated_date"] = updated.dt.strftime("%Y-%m-%d")
    return df.reset_index(drop=True)


DF = load_data()

REGIONS = sorted(DF["region"].unique())
PRODUCTS = sorted(DF["product"].unique())

SYSTEM = f"""You answer questions about the Acme Appliances order ledger by writing Python (pandas).
A DataFrame `df` is already loaded (one row per order, already de-duplicated to the latest version of each order id). `pd` is imported.

Columns:
- id, customer (like 'C1106'), region, product, qty, unit_price, amount, currency (USD/EUR/INR)
- usd: the order amount already converted to USD (use this for all money)
- status: 'paid', 'refunded' or 'void'
- created_at, updated_at: tz-aware datetimes in Asia/Kolkata
- month ('YYYY-MM'), date ('YYYY-MM-DD'): taken from created_at (the order date, Asia/Kolkata)
- updated_month, updated_date: same but from updated_at

Regions: {REGIONS}
Products: {PRODUCTS}

Business rules:
- Revenue = sum of usd where status == 'paid'. Refunded and void orders are NOT revenue.
- Refund amount = sum of usd where status == 'refunded'; refund count = number of such orders.
- Unless the question explicitly talks about when something was updated/refunded, filter dates using month/date (order date).
- Use exact spellings of regions, products and customer ids from the data.

Write code that ends by setting a variable `answer`:
- money -> float rounded to 2 decimals
- counts -> int
- "which product/customer/region" -> the name as a plain string
- "top N"/"list" -> a Python list of names (ordered)
- percentages -> float rounded to 2 decimals (number only)
Output ONLY the Python code, no explanation, no markdown fences."""


# ---------------------------------------------------------------- llm
def call_llm(messages, timeout):
    r = requests.post(
        f"{BASE}/chat/completions",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"model": MODEL, "messages": messages, "temperature": 0},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def clean_code(text):
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


SAFE_BUILTINS = {
    k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
    for k in ["len", "sum", "min", "max", "round", "abs", "sorted", "list", "dict",
              "set", "str", "int", "float", "bool", "range", "enumerate", "zip",
              "any", "all", "tuple", "isinstance", "print"]
}


def run_code(code):
    env = {"df": DF.copy(), "pd": pd, "__builtins__": SAFE_BUILTINS}
    exec(code, env)
    if "answer" not in env:
        raise ValueError("code did not set `answer`")
    return env["answer"]


def to_plain(x):
    if isinstance(x, pd.Series):
        return [to_plain(v) for v in x.tolist()]
    if isinstance(x, pd.DataFrame):
        return x.to_dict("records")
    if isinstance(x, (list, tuple, set)):
        return [to_plain(v) for v in x]
    if isinstance(x, dict):
        return {str(k): to_plain(v) for k, v in x.items()}
    if hasattr(x, "item"):          # numpy scalar
        x = x.item()
    if isinstance(x, float):
        return round(x, 2)
    return x


def answer_question(question, budget=13.0):
    start = time.time()
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": question}]
    last_err = None
    for _ in range(2):
        remaining = budget - (time.time() - start)
        if remaining < 2:
            break
        try:
            code = clean_code(call_llm(messages, timeout=min(remaining - 1, 9)))
            return to_plain(run_code(code))
        except Exception as e:      # retry once, telling the model what failed
            last_err = e
            messages += [{"role": "assistant", "content": "(previous code failed)"},
                         {"role": "user", "content": f"That failed with: {e}. Fix it and output only the code."}]
    raise RuntimeError(last_err)


# ---------------------------------------------------------------- route
@bp.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


@bp.route("/ledger", methods=["POST", "OPTIONS"])
def ledger():
    if request.method == "OPTIONS":
        return ("", 204)
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "missing question"}), 400
    try:
        return jsonify({"answer": answer_question(question)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
