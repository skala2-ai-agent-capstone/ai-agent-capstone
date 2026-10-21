"""기준 견적 계산 (명세 4.3). LLM이 금액을 직접 계산하지 않도록 서버가 계산한다."""
from __future__ import annotations

from fractions import Fraction


def _tier(pricing: dict, pct: Fraction) -> dict:
    for t in sorted(pricing["discount_rules"]["approval_tiers"], key=lambda t: t["max_pct"]):
        if pct <= t["max_pct"]:
            return t
    return pricing["discount_rules"]["approval_tiers"][-1]


def _num(x: Fraction):
    return int(x) if x.denominator == 1 else float(x)


def applied_table(pricing: dict, on_date: str) -> dict:
    day = (on_date or "")[:10]
    cands = [
        t
        for t in pricing["price_tables"]
        if t["status"] == "현행"
        and (t.get("effective_from") or "") <= day
        and (not t.get("effective_to") or day <= t["effective_to"])
    ]
    if not cands:
        cands = [t for t in pricing["price_tables"] if t["status"] == "현행"]
    return max(cands, key=lambda t: t.get("effective_from") or "")


def compute(pricing: dict, qd: dict, completed_at: str) -> dict:
    products = {p["code"]: p for p in pricing["products"]}
    tables = {t["version"]: t for t in pricing["price_tables"]}
    applied = applied_table(pricing, completed_at)
    prices = applied["prices"]
    warnings: list[dict] = []
    quoted = qd.get("price_table")
    if quoted and quoted != applied["version"]:
        warnings.append({
            "code": "PRICE_TABLE_MISMATCH",
            "quoted_price_table": quoted,
            "status": tables[quoted]["status"] if quoted in tables else "미등록",
            "applied_price_table": applied["version"],
        })

    lines = []
    for l in qd.get("lines", []):
        code = l["code"]
        if code not in prices:
            warnings.append({"code": "UNKNOWN_PRODUCT", "product": code})
            continue
        unit = products.get(code, {}).get("unit", "")
        lines.append({
            "code": code,
            "name": products.get(code, {}).get("name"),
            "unit": unit,
            "unit_price": prices[code],
            "quantity": l["quantity"],
            "months": l["months"],
            "period_unit": "회" if unit == "회" else "개월",
            "amount": prices[code] * l["quantity"] * l["months"],
            "free": False,
        })
    for f in qd.get("free_items", []):
        code = f["code"]
        lines.append({
            "code": code,
            "name": products.get(code, {}).get("name"),
            "unit": products.get(code, {}).get("unit", ""),
            "unit_price": 0,
            "quantity": f.get("quantity", 1),
            "months": f.get("months"),
            "amount": 0,
            "free": True,
        })
    subtotal = sum(l["amount"] for l in lines)

    term_months = max([l["months"] for l in lines if not l["free"] and l["period_unit"] == "개월"] or [0])
    p100_qty = sum(l["quantity"] for l in lines if not l["free"] and l["code"] == "P-100")
    checks, recognized, unrecognized = [], Fraction(0), []
    unrec_pct = Fraction(0)
    for c in qd.get("discount_components", []):
        pct = Fraction(str(c["pct"]))
        t = c["type"]
        if t == "다년":
            rule = 5 if term_months >= 60 else 3 if term_months >= 36 else 0
            checks.append({"type": t, "pct": _num(pct), "rule_pct": rule, "eligible": rule > 0 and pct == rule,
                           "basis": f"계약 기간 {term_months}개월"})
            recognized += pct
        elif t == "수량":
            rule = 2 if p100_qty >= 500 else 0
            checks.append({"type": t, "pct": _num(pct), "rule_pct": rule, "eligible": rule > 0 and pct == rule,
                           "basis": f"P-100 사용자 {p100_qty}명"})
            recognized += pct
        elif t == "고객 요청 추가":
            checks.append({"type": t, "pct": _num(pct), "rule_pct": None, "eligible": None, "basis": "표준 외 추가 할인 (승인 단계로 통제)"})
            recognized += pct
        else:
            unrecognized.append(t)
            unrec_pct += pct
            checks.append({"type": t, "pct": _num(pct), "rule_pct": None, "eligible": False, "basis": "할인 규칙에 없는 유형"})

    def total_of(pct: Fraction) -> int:
        v = Fraction(subtotal) * (100 - pct) / 100
        return int(v // 1000) * 1000

    total = total_of(recognized)
    targets = []
    tier = _tier(pricing, recognized)
    if tier.get("approval_required"):
        targets.append({"type": "할인", "approver": tier["approver"], "basis": f"합산 할인율 {_num(recognized)}%"})
    exc_map = {e["type"]: e["approver"] for e in pricing["discount_rules"]["exceptions"]}
    for f in qd.get("free_items", []):
        targets.append({"type": "무상 제공", "approver": exc_map.get("무상 제공", "담당 임원"), "basis": f["code"]})
    for e in qd.get("exceptions", []):
        approver = exc_map.get(e["type"])
        if approver is None:
            # '표준 계약서 외 조항'처럼 가격표 유형명 일부로 적힌 경우
            approver = next((v for k, v in exc_map.items() if e["type"] in k or k in e["type"]), None)
        if approver is None:
            warnings.append({"code": "UNKNOWN_EXCEPTION_TYPE", "type": e["type"]})
            continue
        targets.append({"type": e["type"], "approver": approver, "basis": e.get("value")})

    out = {
        "applied_price_table": applied["version"],
        "quoted_price_table": quoted,
        "lines": lines,
        "subtotal": subtotal,
        "component_checks": checks,
        "discount_pct": _num(recognized),
        "total": total,
        "vat": "별도",
        "rounding": pricing["discount_rules"].get("rounding"),
        "approval_targets": targets,
        "unrecognized_components": unrecognized,
        "warnings": warnings,
        "note": "승인 여부는 계산하지 않는다. 승인 요청은 get_approval_requests로 확인",
    }
    if unrecognized:
        out["total_with_unrecognized"] = total_of(recognized + unrec_pct)
    return out
