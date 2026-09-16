#!/usr/bin/env python3
"""
generate_loan_packets.py — synthetic loan application packets for a Dataloop demo.

Produces realistic-looking (but entirely fake) PDFs across five document types,
plus ground truth, model-style pre-labels with seeded errors, and an upload
manifest. No real people, no PII, safe to show a bank.

  python generate_loan_packets.py --packets 120 --out ./demo_data

Outputs under --out:
  base/                 fully-labeled ground truth split (Act 1 / Act 4 training)
  unlabeled/            annotation task split (Act 2)
  incoming/             trigger folder for the live pipeline (Act 3)
  ground_truth.json     true field values for every document
  prelabels.json        model-style predictions w/ confidence + seeded errors
  manifest.csv          filename, split, loan_id, doc_type, scanned, page_count
  dataloop_metadata.json  filename -> item metadata dict, ready for SDK upload
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from faker import Faker
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as rl_canvas

DOC_TYPES = [
    "loan_application",
    "pay_stub",
    "bank_statement",
    "w2",
    "id_verification",
]

BANKS = [
    "Northgate Federal Savings",
    "Cambria Trust Bank",
    "Meridian Community Bank",
    "Halstead National",
    "Pinebrook Credit Union",
]

EMPLOYERS = [
    "Vantage Logistics LLC",
    "Corbin Manufacturing Inc.",
    "Delmar Health Partners",
    "Rowan Digital Services",
    "Tessera Foods Group",
]

LOAN_PURPOSES = ["Home purchase", "Refinance", "Home improvement", "Debt consolidation"]

W, H = LETTER
M = 0.85 * inch  # margin


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@dataclass
class Doc:
    loan_id: str
    doc_type: str
    fields: dict
    path: Path = None
    scanned: bool = False
    pages: int = 1


def money(v: float) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def rule(c, y, x0=M, x1=W - M, width=0.6, gray=0.55):
    c.setStrokeGray(gray)
    c.setLineWidth(width)
    c.line(x0, y, x1, y)
    c.setStrokeGray(0)


def header(c, title, subtitle=None, org=None):
    c.setFont("Helvetica-Bold", 15)
    c.drawString(M, H - M, org or "")
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - M, H - M, title)
    if subtitle:
        c.setFont("Helvetica", 8.5)
        c.setFillGray(0.35)
        c.drawRightString(W - M, H - M - 13, subtitle)
        c.setFillGray(0)
    rule(c, H - M - 22)
    return H - M - 45


def kv_block(c, y, pairs, label_w=150, line_h=17, font_size=9.5):
    for label, value in pairs:
        c.setFont("Helvetica", font_size)
        c.setFillGray(0.4)
        c.drawString(M, y, label)
        c.setFillGray(0)
        c.setFont("Helvetica-Bold", font_size)
        c.drawString(M + label_w, y, str(value))
        y -= line_h
    return y


def section(c, y, text):
    y -= 8
    c.setFont("Helvetica-Bold", 10)
    c.drawString(M, y, text.upper())
    rule(c, y - 5, x1=M + 190, gray=0.2, width=1.1)
    return y - 24


def footer(c, text):
    c.setFont("Helvetica-Oblique", 7)
    c.setFillGray(0.55)
    c.drawString(M, 0.55 * inch, text)
    c.drawRightString(W - M, 0.55 * inch, "SYNTHETIC DOCUMENT — NOT A REAL RECORD")
    c.setFillGray(0)


# --------------------------------------------------------------------------
# document renderers — each returns a fields dict of extractable ground truth
# --------------------------------------------------------------------------


def make_loan_application(c, fk, ctx):
    f = {
        "applicant_name": ctx["name"],
        "loan_id": ctx["loan_id"],
        "application_date": ctx["app_date"].strftime("%m/%d/%Y"),
        "loan_amount": money(ctx["loan_amount"]),
        "loan_purpose": ctx["purpose"],
        "property_address": ctx["address"],
        "annual_income": money(ctx["annual_income"]),
        "employer_name": ctx["employer"],
        "years_employed": f"{ctx['years_employed']}",
        "credit_score": str(ctx["credit_score"]),
        "monthly_debt": money(ctx["monthly_debt"]),
        "term_months": str(ctx["term_months"]),
    }

    y = header(c, "UNIFORM LOAN APPLICATION", f"Application {ctx['loan_id']}", ctx["bank"])
    y = section(c, y, "Borrower information")
    y = kv_block(c, y, [
        ("Full legal name", f["applicant_name"]),
        ("Date of birth", ctx["dob"].strftime("%m/%d/%Y")),
        ("Current address", f["property_address"]),
        ("Phone", ctx["phone"]),
        ("Email", ctx["email"]),
    ])
    y = section(c, y, "Employment and income")
    y = kv_block(c, y, [
        ("Employer name", f["employer_name"]),
        ("Position", ctx["job_title"]),
        ("Years employed", f["years_employed"]),
        ("Gross annual income", f["annual_income"]),
        ("Other monthly income", money(ctx["other_income"])),
    ])
    y = section(c, y, "Loan request")
    y = kv_block(c, y, [
        ("Amount requested", f["loan_amount"]),
        ("Purpose of loan", f["loan_purpose"]),
        ("Requested term", f"{f['term_months']} months"),
        ("Monthly debt obligations", f["monthly_debt"]),
        ("Reported credit score", f["credit_score"]),
        ("Application date", f["application_date"]),
    ])

    y = section(c, y, "Declarations")
    c.setFont("Helvetica", 8.2)
    c.setFillGray(0.25)
    for line in [
        "The undersigned certifies that the information provided in this application is true and complete.",
        "The lender is authorized to verify employment, income, and deposit balances stated above.",
        "This document is generated test data and has no legal effect.",
    ]:
        c.drawString(M, y, line)
        y -= 12
    c.setFillGray(0)

    y -= 26
    rule(c, y, x1=M + 230, gray=0.3)
    rule(c, y, x0=W - M - 150, x1=W - M, gray=0.3)
    c.setFont("Helvetica", 8)
    c.setFillGray(0.45)
    c.drawString(M, y - 11, "Borrower signature")
    c.drawString(W - M - 150, y - 11, "Date")
    c.setFillGray(0)
    c.setFont("Helvetica-Oblique", 13)
    c.drawString(M + 6, y + 6, ctx["name"])
    c.setFont("Helvetica", 9)
    c.drawString(W - M - 144, y + 6, f["application_date"])

    footer(c, f"{ctx['bank']} · Form UL-1003 · {ctx['loan_id']}")
    return f


def make_pay_stub(c, fk, ctx, period_index=0):
    period_end = ctx["app_date"] - timedelta(days=14 * (period_index + 1))
    period_start = period_end - timedelta(days=13)
    gross = round(ctx["annual_income"] / 26, 2)
    fed = round(gross * 0.142, 2)
    state = round(gross * 0.049, 2)
    fica = round(gross * 0.0765, 2)
    health = round(random.uniform(60, 190), 2)
    retire = round(gross * random.choice([0.03, 0.05, 0.06]), 2)
    net = round(gross - fed - state - fica - health - retire, 2)
    ytd_gross = round(gross * (period_index + random.randint(8, 20)), 2)

    f = {
        "employee_name": ctx["name"],
        "employer_name": ctx["employer"],
        "pay_period_start": period_start.strftime("%m/%d/%Y"),
        "pay_period_end": period_end.strftime("%m/%d/%Y"),
        "gross_pay": money(gross),
        "net_pay": money(net),
        "federal_tax": money(fed),
        "ytd_gross": money(ytd_gross),
        "employee_id": ctx["employee_id"],
    }

    y = header(c, "EARNINGS STATEMENT",
               f"Period {f['pay_period_start']} – {f['pay_period_end']}", ctx["employer"])
    y = kv_block(c, y, [
        ("Employee", f["employee_name"]),
        ("Employee ID", f["employee_id"]),
        ("Pay date", (period_end + timedelta(days=5)).strftime("%m/%d/%Y")),
        ("Pay frequency", "Bi-weekly"),
    ])

    y = section(c, y, "Earnings")
    c.setFont("Helvetica-Bold", 8.5)
    cols = [M, M + 210, M + 300, M + 400]
    for x, t in zip(cols, ["DESCRIPTION", "RATE", "HOURS", "CURRENT"]):
        c.drawString(x, y, t)
    rule(c, y - 4)
    y -= 17
    rate = round(ctx["annual_income"] / 2080, 2)
    rows = [("Regular", money(rate), "80.00", money(round(rate * 80, 2)))]
    if random.random() < 0.4:
        ot = round(random.uniform(2, 9), 2)
        rows.append(("Overtime", money(round(rate * 1.5, 2)), f"{ot:.2f}",
                     money(round(rate * 1.5 * ot, 2))))
    c.setFont("Helvetica", 9)
    for r in rows:
        for x, t in zip(cols, r):
            c.drawString(x, y, t)
        y -= 15
    rule(c, y + 5)
    y -= 6
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(cols[0], y, "Gross pay")
    c.drawString(cols[3], y, f["gross_pay"])
    y -= 10

    y = section(c, y, "Deductions")
    c.setFont("Helvetica", 9)
    for label, amt in [("Federal income tax", fed), ("State income tax", state),
                       ("Social Security / Medicare", fica), ("Health insurance", health),
                       ("401(k) contribution", retire)]:
        c.drawString(cols[0], y, label)
        c.drawString(cols[3], y, money(amt))
        y -= 15
    rule(c, y + 5)
    y -= 8
    c.setFont("Helvetica-Bold", 11)
    c.drawString(cols[0], y, "NET PAY")
    c.drawString(cols[3], y, f["net_pay"])

    y -= 34
    c.setFont("Helvetica", 8.5)
    c.setFillGray(0.4)
    c.drawString(M, y, f"Year-to-date gross: {f['ytd_gross']}    "
                       f"Year-to-date net: {money(round(ytd_gross * 0.71, 2))}")
    c.setFillGray(0)

    footer(c, f"{ctx['employer']} · Payroll services")
    return f


def make_bank_statement(c, fk, ctx):
    end = ctx["app_date"].replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    opening = round(random.uniform(1800, 24000), 2)
    balance = opening
    txns = []
    merchants = ["POS PURCHASE - GROCERY", "ACH DEBIT - UTILITY", "CARD PURCHASE - FUEL",
                 "ONLINE TRANSFER", "ATM WITHDRAWAL", "CARD PURCHASE - PHARMACY",
                 "ACH DEBIT - INSURANCE", "CARD PURCHASE - RESTAURANT"]
    n = random.randint(14, 20)
    for i in range(n):
        d = start + timedelta(days=int(i * ((end - start).days) / n))
        if i in (2, 11):
            desc = f"DIRECT DEPOSIT - {ctx['employer'][:22].upper()}"
            amt = round(ctx["annual_income"] / 26 * 0.72, 2)
        else:
            desc = random.choice(merchants)
            amt = -round(random.uniform(18, 640), 2)
        balance = round(balance + amt, 2)
        txns.append((d, desc, amt, balance))

    deposits = round(sum(a for _, _, a, _ in txns if a > 0), 2)
    withdrawals = round(-sum(a for _, _, a, _ in txns if a < 0), 2)

    f = {
        "account_holder": ctx["name"],
        "bank_name": ctx["bank"],
        "account_number": ctx["account_number"],
        "statement_period": f"{start.strftime('%m/%d/%Y')} – {end.strftime('%m/%d/%Y')}",
        "opening_balance": money(opening),
        "closing_balance": money(balance),
        "total_deposits": money(deposits),
        "total_withdrawals": money(withdrawals),
    }

    y = header(c, "ACCOUNT STATEMENT", f["statement_period"], ctx["bank"])
    y = kv_block(c, y, [
        ("Account holder", f["account_holder"]),
        ("Account number", f["account_number"]),
        ("Account type", "Personal Checking"),
        ("Branch", ctx["branch"]),
    ])

    y = section(c, y, "Summary")
    y = kv_block(c, y, [
        ("Opening balance", f["opening_balance"]),
        ("Total deposits", f["total_deposits"]),
        ("Total withdrawals", f["total_withdrawals"]),
        ("Closing balance", f["closing_balance"]),
    ])

    y = section(c, y, "Transaction detail")
    c.setFont("Helvetica-Bold", 8)
    cols = [M, M + 75, M + 330, M + 425]
    for x, t in zip(cols, ["DATE", "DESCRIPTION", "AMOUNT", "BALANCE"]):
        c.drawString(x, y, t)
    rule(c, y - 4)
    y -= 14
    c.setFont("Courier", 8)
    for d, desc, amt, bal in txns:
        if y < 1.15 * inch:
            footer(c, f"{ctx['bank']} · Statement continues")
            c.showPage()
            y = header(c, "ACCOUNT STATEMENT (continued)", f["statement_period"], ctx["bank"])
            c.setFont("Courier", 8)
        c.drawString(cols[0], y, d.strftime("%m/%d/%y"))
        c.drawString(cols[1], y, desc[:44])
        c.drawRightString(cols[2] + 60, y, money(amt))
        c.drawRightString(cols[3] + 70, y, money(bal))
        y -= 12.5

    footer(c, f"{ctx['bank']} · Member FDIC (simulated)")
    return f


def make_w2(c, fk, ctx):
    wages = round(ctx["annual_income"], 2)
    f = {
        "employee_name": ctx["name"],
        "employer_name": ctx["employer"],
        "tax_year": str(ctx["app_date"].year - 1),
        "wages_tips_other": money(wages),
        "federal_income_tax_withheld": money(round(wages * 0.141, 2)),
        "social_security_wages": money(min(wages, 168600.0)),
        "employer_ein": ctx["ein"],
        "control_number": ctx["employee_id"],
    }

    y = header(c, f"FORM W-2  ·  WAGE AND TAX STATEMENT", f"Tax year {f['tax_year']}",
               ctx["employer"])

    boxes = [
        ("a  Employee identification", ctx["employee_id"]),
        ("b  Employer EIN", f["employer_ein"]),
        ("c  Employer name and address", ctx["employer"]),
        ("e  Employee name", f["employee_name"]),
        ("f  Employee address", ctx["address"]),
    ]
    y = kv_block(c, y, boxes, label_w=210)

    y = section(c, y, "Wage and withholding boxes")
    grid = [
        ("1  Wages, tips, other compensation", f["wages_tips_other"]),
        ("2  Federal income tax withheld", f["federal_income_tax_withheld"]),
        ("3  Social security wages", f["social_security_wages"]),
        ("4  Social security tax withheld", money(round(wages * 0.062, 2))),
        ("5  Medicare wages and tips", f["wages_tips_other"]),
        ("6  Medicare tax withheld", money(round(wages * 0.0145, 2))),
        ("12a Deferred compensation (D)", money(round(wages * 0.05, 2))),
        ("16 State wages, tips, etc.", f["wages_tips_other"]),
        ("17 State income tax", money(round(wages * 0.048, 2))),
    ]
    box_w, box_h = (W - 2 * M) / 2 - 6, 40
    for i, (label, value) in enumerate(grid):
        col, row = i % 2, i // 2
        x = M + col * (box_w + 12)
        yy = y - row * (box_h + 8)
        c.setStrokeGray(0.6)
        c.rect(x, yy - box_h + 12, box_w, box_h, stroke=1, fill=0)
        c.setStrokeGray(0)
        c.setFont("Helvetica", 7)
        c.setFillGray(0.4)
        c.drawString(x + 5, yy + 1, label)
        c.setFillGray(0)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x + 5, yy - 16, str(value))

    footer(c, "Synthetic W-2 for demonstration · not filed with any authority")
    return f


def make_id_verification(c, fk, ctx):
    # Most IDs are current; ~10% are expired at application time, which should
    # flunk the expiry check and force a manual review. That mismatch is a
    # useful thing for an annotator to catch in the demo.
    issued = ctx["app_date"] - timedelta(days=random.randint(200, 2900))
    expires = issued + timedelta(days=365 * 8)
    if random.random() < 0.10:
        issued = ctx["app_date"] - timedelta(days=random.randint(3000, 3600))
        expires = issued + timedelta(days=365 * 8 - random.randint(200, 900))
    expired = expires < ctx["app_date"]
    f = {
        "full_name": ctx["name"],
        "date_of_birth": ctx["dob"].strftime("%m/%d/%Y"),
        "document_number": ctx["id_number"],
        "issuing_state": ctx["state"],
        "issue_date": issued.strftime("%m/%d/%Y"),
        "expiry_date": expires.strftime("%m/%d/%Y"),
        "address": ctx["address"],
        "verification_result": "REVIEW" if expired
        else random.choices(["PASS", "REVIEW"], weights=[88, 12])[0],
    }

    y = header(c, "IDENTITY VERIFICATION RECORD", f"KYC reference {ctx['kyc_ref']}", ctx["bank"])

    # ID card mock
    card_h = 150
    c.setStrokeGray(0.55)
    c.setFillGray(0.965)
    c.roundRect(M, y - card_h, 340, card_h, 8, stroke=1, fill=1)
    c.setFillGray(0)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(M + 14, y - 22, f"{ctx['state'].upper()} DRIVER LICENSE")
    c.setFillGray(0.85)
    c.rect(M + 14, y - 122, 72, 88, stroke=0, fill=1)
    c.setFillGray(0.5)
    c.setFont("Helvetica", 7)
    c.drawCentredString(M + 50, y - 80, "PHOTO")
    c.setFillGray(0)
    yy = y - 42
    for label, val in [("DLN", f["document_number"]), ("NAME", f["full_name"]),
                       ("DOB", f["date_of_birth"]), ("ISS", f["issue_date"]),
                       ("EXP", f["expiry_date"])]:
        c.setFont("Helvetica", 7)
        c.setFillGray(0.45)
        c.drawString(M + 100, yy, label)
        c.setFillGray(0)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(M + 128, yy, str(val)[:30])
        yy -= 16

    y = y - card_h - 24
    y = section(c, y, "Verification checks")
    checks = [
        ("Document authenticity", "PASS"),
        ("Facial similarity", "PASS" if f["verification_result"] == "PASS" else "REVIEW"),
        ("Address match", "PASS"),
        ("Sanctions / PEP screening", "NO MATCH"),
        ("Document expiry", "EXPIRED" if expired else "VALID"),
    ]
    y = kv_block(c, y, checks, label_w=210)

    y -= 10
    c.setFont("Helvetica-Bold", 11)
    c.drawString(M, y, f"OVERALL RESULT: {f['verification_result']}")

    footer(c, f"{ctx['bank']} · Financial crime operations · synthetic record")
    return f


RENDERERS = {
    "loan_application": make_loan_application,
    "pay_stub": make_pay_stub,
    "bank_statement": make_bank_statement,
    "w2": make_w2,
    "id_verification": make_id_verification,
}


# --------------------------------------------------------------------------
# scan degradation
# --------------------------------------------------------------------------


def degrade_to_scan(path: Path, dpi=150):
    """Rasterize, skew, add noise, and rewrite as an image-only PDF."""
    import pymupdf
    from PIL import Image, ImageFilter

    src = pymupdf.open(path)
    out = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        img = img.rotate(random.uniform(-1.1, 1.1), expand=False,
                         fillcolor=(252, 251, 248), resample=Image.BICUBIC)
        img = img.filter(ImageFilter.GaussianBlur(random.uniform(0.3, 0.8)))
        px = img.load()
        for _ in range(int(img.width * img.height * 0.004)):
            x, y = random.randrange(img.width), random.randrange(img.height)
            g = random.randint(90, 200)
            px[x, y] = (g, g, g)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=random.randint(58, 78))
        rect = pymupdf.Rect(0, 0, page.rect.width, page.rect.height)
        np = out.new_page(width=rect.width, height=rect.height)
        np.insert_image(rect, stream=buf.getvalue())
    src.close()
    tmp = path.with_suffix(".tmp.pdf")
    out.save(tmp)
    out.close()
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# packet assembly
# --------------------------------------------------------------------------


def build_context(fk: Faker) -> dict:
    dob = fk.date_between(date(1962, 1, 1), date(1999, 12, 31))
    app_date = fk.date_between(date(2025, 1, 1), date(2026, 6, 30))
    income = round(random.uniform(41000, 205000), -2)
    return {
        "loan_id": f"LN-{random.randint(100000, 999999)}",
        "name": fk.name(),
        "dob": dob,
        "app_date": app_date,
        "address": f"{fk.street_address()}, {fk.city()}, {fk.state_abbr()} {fk.postcode()}",
        "state": fk.state(),
        "phone": fk.phone_number(),
        "email": fk.email(),
        "bank": random.choice(BANKS),
        "branch": f"{fk.city()} Branch",
        "employer": random.choice(EMPLOYERS),
        "job_title": fk.job()[:38],
        "years_employed": random.randint(1, 18),
        "annual_income": income,
        "other_income": round(random.uniform(0, 1800), 2),
        "loan_amount": round(random.uniform(25000, 640000), -3),
        "purpose": random.choice(LOAN_PURPOSES),
        "term_months": random.choice([120, 180, 240, 360]),
        "credit_score": random.randint(580, 815),
        "monthly_debt": round(random.uniform(250, 3200), 2),
        "account_number": f"****{random.randint(1000, 9999)}",
        "employee_id": f"EMP-{random.randint(10000, 99999)}",
        "ein": f"{random.randint(10, 99)}-{random.randint(1000000, 9999999)}",
        "id_number": f"{random.choice('ABCDEFGHJKMNPQRSTVWXYZ')}{random.randint(1000000, 9999999)}",
        "kyc_ref": f"KYC-{random.randint(100000, 999999)}",
    }


def packet_plan(ctx) -> list:
    plan = [("loan_application", {}), ("bank_statement", {}), ("id_verification", {})]
    for i in range(random.randint(1, 2)):
        plan.append(("pay_stub", {"period_index": i}))
    if random.random() < 0.8:
        plan.append(("w2", {}))
    return plan


def render(doc_type, ctx, fk, out_path: Path, **kw):
    c = rl_canvas.Canvas(str(out_path), pagesize=LETTER)
    c.setTitle(f"{doc_type} {ctx['loan_id']}")
    fields = RENDERERS[doc_type](c, fk, ctx, **kw)
    c.showPage()
    c.save()
    return fields


# --------------------------------------------------------------------------
# pre-labels with seeded errors
# --------------------------------------------------------------------------


def perturb(value: str) -> str:
    v = str(value)
    if v.startswith("$"):
        digits = [ch for ch in v if ch.isdigit()]
        if len(digits) >= 2:
            i = random.randrange(len(v))
            if v[i].isdigit():
                return v[:i] + str((int(v[i]) + random.choice([1, 2, 3, 7])) % 10) + v[i + 1:]
        return v
    if "/" in v and len(v) >= 8:
        parts = v.split("/")
        if parts[0].isdigit():
            parts[0] = f"{max(1, (int(parts[0]) % 12) + 1):02d}"
            return "/".join(parts)
    if " " in v:
        words = v.split()
        random.shuffle(words)
        return " ".join(words)
    return v[:-1] + random.choice("XY8") if len(v) > 2 else v


def make_prelabels(fields: dict, error_rate: float) -> dict:
    out = {}
    for k, v in fields.items():
        wrong = random.random() < error_rate
        val = perturb(v) if wrong else v
        conf = round(random.uniform(0.42, 0.74), 3) if wrong else round(random.uniform(0.78, 0.995), 3)
        out[k] = {"value": val, "confidence": conf, "seeded_error": wrong}
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packets", type=int, default=120, help="number of loan packets")
    ap.add_argument("--out", default="./demo_data", help="output directory")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scan-ratio", type=float, default=0.25,
                    help="fraction of documents degraded to scanned image-only PDFs")
    ap.add_argument("--split", default="60,30,10",
                    help="percent split for base,unlabeled,incoming")
    ap.add_argument("--error-rate", type=float, default=0.12,
                    help="fraction of pre-label fields that are deliberately wrong")
    args = ap.parse_args()

    random.seed(args.seed)
    fk = Faker()
    Faker.seed(args.seed)

    out = Path(args.out)
    splits = [int(x) for x in args.split.split(",")]
    assert len(splits) == 3 and sum(splits) == 100, "--split must be three ints summing to 100"
    names = ["base", "unlabeled", "incoming"]
    for n in names:
        (out / n).mkdir(parents=True, exist_ok=True)

    ground_truth, prelabels, manifest = {}, {}, []
    docs = []

    # Exact split assignment — a random draw leaves the small `incoming` bucket
    # too thin to demo with at typical packet counts.
    n_base = round(args.packets * splits[0] / 100)
    n_unlab = round(args.packets * splits[1] / 100)
    assignment = ([names[0]] * n_base + [names[1]] * n_unlab)
    assignment += [names[2]] * (args.packets - len(assignment))
    random.shuffle(assignment)

    for p in range(args.packets):
        ctx = build_context(fk)
        split = assignment[p]
        for doc_type, kw in packet_plan(ctx):
            suffix = f"_{kw['period_index'] + 1}" if doc_type == "pay_stub" else ""
            fname = f"{ctx['loan_id']}__{doc_type}{suffix}.pdf"
            path = out / split / fname
            fields = render(doc_type, ctx, fk, path, **kw)
            scanned = random.random() < args.scan_ratio
            if scanned:
                degrade_to_scan(path)
            docs.append(Doc(ctx["loan_id"], doc_type, fields, path, scanned))
            ground_truth[fname] = {
                "loan_id": ctx["loan_id"], "doc_type": doc_type,
                "split": split, "scanned": scanned, "fields": fields,
            }
            if split in ("unlabeled", "incoming"):
                prelabels[fname] = {
                    "loan_id": ctx["loan_id"], "doc_type": doc_type,
                    "predictions": make_prelabels(fields, args.error_rate),
                }
            manifest.append({
                "filename": fname, "split": split, "loan_id": ctx["loan_id"],
                "doc_type": doc_type, "scanned": scanned,
                "applicant": ctx["name"], "bank": ctx["bank"],
            })
        if (p + 1) % 25 == 0:
            print(f"  {p + 1}/{args.packets} packets")

    (out / "ground_truth.json").write_text(json.dumps(ground_truth, indent=2))
    (out / "prelabels.json").write_text(json.dumps(prelabels, indent=2))

    dl_meta = {
        m["filename"]: {
            "user": {
                "loan_id": m["loan_id"], "doc_type": m["doc_type"],
                "scanned": m["scanned"], "bank": m["bank"], "split": m["split"],
            }
        } for m in manifest
    }
    (out / "dataloop_metadata.json").write_text(json.dumps(dl_meta, indent=2))

    with open(out / "manifest.csv", "w", newline="") as fh:
        wtr = csv.DictWriter(fh, fieldnames=list(manifest[0].keys()))
        wtr.writeheader()
        wtr.writerows(manifest)

    counts = {}
    for m in manifest:
        counts[m["split"]] = counts.get(m["split"], 0) + 1
    scanned_n = sum(1 for m in manifest if m["scanned"])
    seeded = sum(1 for v in prelabels.values()
                 for p in v["predictions"].values() if p["seeded_error"])

    print(f"\n{len(manifest)} documents from {args.packets} packets -> {out}")
    for n in names:
        print(f"  {n:<10} {counts.get(n, 0):>5}")
    print(f"  scanned    {scanned_n:>5}  (image-only, needs OCR)")
    print(f"  seeded pre-label errors: {seeded}")
    print("\nNext: upload with upload_to_dataloop.py")


if __name__ == "__main__":
    main()
