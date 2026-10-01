"""Acme CoreOne Teller 4.2 -- a deliberately "legacy" mock core-banking app.

This is the proxy target for the computer-use system. It imitates the parts of
real back-office software that make automation hard:

* a frameset (banner / menu / work frames), so content lives in nested frames
* table-based layouts: input labels sit in a neighbouring <td>, not a <label>
* no ids or test ids; form fields carry generated names like ``f_0012``
* server-rendered, uppercase, <font>-styled screens with no headings
* runtime exceptional states that can be injected via ``/__admin/fault``:
  session timeout, system-notice overlay, "processing" interim page,
  transaction ABEND, and an unexpected security-verification screen.

``variant="harbor"`` serves the same vendor product configured differently
(a second tenant): different branding, a relabelled member-number field, and
the "Shares" tab renamed to "Accounts".
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass

from flask import Flask, abort, jsonify, redirect, render_template_string, request, session

from .data import MEMBERS, OPERATORS


@dataclass(frozen=True)
class Variant:
    brand: str
    member_label: str
    shares_tab: str


VARIANTS = {
    "acme": Variant(brand="ACME FEDERAL CREDIT UNION", member_label="Member Number", shares_tab="Shares"),
    "harbor": Variant(brand="HARBOR COMMUNITY CU", member_label="Account No.", shares_tab="Accounts"),
}

FAULT_NAMES = {"session_expired", "system_notice", "processing", "abend", "security_check"}

PAGE = """<html><head><title>CoreOne Teller</title>
<style>body{font-family:'Courier New',monospace;font-size:13px;background:#d8d8c8;margin:6px}
td{font-family:'Courier New',monospace;font-size:13px}</style></head>
<body>{{ body|safe }}</body></html>"""


def create_app(variant: str = "acme") -> Flask:
    v = VARIANTS[variant]
    app = Flask(__name__)
    app.secret_key = f"mockbank-{variant}-not-a-real-secret"
    app.config["SESSION_COOKIE_NAME"] = f"coreone_{variant}"

    faults: dict[str, dict] = {}
    opened_shares: dict[str, list[dict]] = {}
    lock = threading.Lock()

    def page(body: str, status: int = 200):
        return render_template_string(PAGE, body=body), status

    def take_fault(name: str, path: str) -> bool:
        with lock:
            f = faults.get(name)
            if not f or f["remaining"] <= 0 or not path.startswith(f["route"]):
                return False
            f["remaining"] -= 1
            return True

    def logged_in() -> bool:
        return bool(session.get("op"))

    def screen(title: str, inner: str) -> str:
        return (
            f'<table width="100%" cellpadding=2 bgcolor="#000080"><tr><td>'
            f'<font color="#ffffff"><b>{title}</b></font></td>'
            f'<td align=right><font color="#ffffff">OPR {session.get("op", "-")}</font></td></tr></table>'
            f"<br>{inner}"
        )

    def timeout_page(next_path: str = "/work/home"):
        return page(screen("SESSION TIMED OUT", f"""
<font color=red><b>*** SESSION TIMED OUT - PLEASE SIGN ON AGAIN TO CONTINUE ***</b></font><br><br>
<form method=post action="/work/reauth"><input type=hidden name=next value="{next_path}">
<table cellpadding=3>
<tr><td>Operator ID</td><td><input type=text name=f_0001 size=12></td></tr>
<tr><td>Password</td><td><input type=password name=f_0002 size=12></td></tr>
<tr><td></td><td><input type=submit value="Resume Session"></td></tr>
</table></form>"""))

    # ---------------------------------------------------------------- faults
    @app.before_request
    def inject_faults():
        p = request.path
        if not p.startswith("/work/") or p in ("/work/reauth", "/work/verify-identity"):
            return None
        if not logged_in():
            return timeout_page("/work/home")
        if take_fault("session_expired", p):
            session.pop("op", None)
            return timeout_page(p if request.method == "GET" else "/work/inquiry")
        if take_fault("abend", p):
            return page(screen("SYSTEM ERROR", """
<font color=red><b>SYSTEM ERROR - TRANSACTION ABENDED (CODE S0C7)</b></font><br>
CONTACT THE HELP DESK AT EXT 4400. REFERENCE: TXN-88412."""), 500)
        if request.method == "GET" and take_fault("processing", p):
            return page("""<meta http-equiv="refresh" content="2">
<br><br><center><b>PROCESSING - PLEASE WAIT ...</b></center>""")
        if request.method == "GET" and take_fault("security_check", p):
            return page(screen("SECURITY VERIFICATION", f"""
<b>VERIFY MEMBER IDENTITY BEFORE VIEWING ACCOUNT DETAILS.</b><br>
ASK THE MEMBER FOR THEIR SECURITY WORD AND CONFIRM IT MATCHES THE CARD ON FILE.<br><br>
<form method=post action="/work/verify-identity"><input type=hidden name=next value="{p}">
<input type=submit value="Identity Verified"> <input type=submit name=cancel value="Cancel"></form>"""))
        return None

    @app.post("/__admin/fault")
    def set_fault():
        body = request.get_json(force=True)
        name = body["fault"]
        if name not in FAULT_NAMES:
            abort(400)
        with lock:
            faults[name] = {"remaining": int(body.get("count", 1)), "route": body.get("route", "/work/")}
        return jsonify(ok=True, faults=faults)

    @app.post("/__admin/reset")
    def reset():
        with lock:
            faults.clear()
            opened_shares.clear()
        return jsonify(ok=True)

    # --------------------------------------------------------------- sign-on
    @app.get("/")
    def root():
        return redirect("/signon")

    @app.route("/signon", methods=["GET", "POST"])
    def signon():
        err = ""
        if request.method == "POST":
            op = request.form.get("f_0001", "").upper()
            if OPERATORS.get(op) == request.form.get("f_0002"):
                session["op"] = op
                return redirect("/desk")
            err = "<font color=red><b>*** INVALID OPERATOR ID OR PASSWORD ***</b></font><br><br>"
        return page(f"""
<center><br><font size=+1><b>{v.brand}</b></font><br>COREONE TELLER 4.2<br><br>
<b>OPERATOR SIGN ON</b><br><br>{err}
<form method=post><table cellpadding=3 border=1 bgcolor="#eeeeee">
<tr><td>Operator ID</td><td><input type=text name=f_0001 size=12></td></tr>
<tr><td>Password</td><td><input type=password name=f_0002 size=12></td></tr>
<tr><td colspan=2 align=center><input type=submit value="Sign On"></td></tr>
</table></form></center>""")

    @app.get("/desk")
    def desk():
        if not logged_in():
            return redirect("/signon")
        return """<html><head><title>CoreOne Teller - Desk</title></head>
<frameset rows="40,*" border=1>
  <frame name="banner" src="/banner" scrolling=no>
  <frameset cols="190,*">
    <frame name="menu" src="/menu">
    <frame name="work" src="/work/home">
  </frameset>
</frameset></html>"""

    @app.get("/banner")
    def banner():
        return page(f'<font size=+1><b>{v.brand}</b></font> &nbsp; COREONE TELLER 4.2')

    @app.get("/menu")
    def menu():
        return page("""<table cellpadding=4 width="100%">
<tr><td bgcolor="#000080"><font color=white><b>FUNCTIONS</b></font></td></tr>
<tr><td><a href="/work/inquiry" target="work">Member Inquiry</a></td></tr>
<tr><td><a href="/work/home" target="work">Teller Home</a></td></tr>
<tr><td><font color=gray>Transactions</font></td></tr>
<tr><td><a href="/signoff" target="_top">Sign Off</a></td></tr>
</table>""")

    @app.get("/signoff")
    def signoff():
        session.clear()
        return redirect("/signon")

    # ------------------------------------------------------------ work frame
    def notice_overlay() -> str:
        if not take_fault("system_notice", request.path):
            return ""
        return """<div style="position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.45)">
<table style="margin:80px auto" border=2 cellpadding=8 bgcolor="#ffffcc">
<tr><td><b>SYSTEM NOTICE</b></td></tr>
<tr><td>END-OF-DAY PROCESSING BEGINS AT 18:00. POSTINGS AFTER 17:45 WILL BE DATED NEXT BUSINESS DAY.</td></tr>
<tr><td align=center><input type=button value="OK" onclick="this.closest('div').remove()"></td></tr>
</table></div>"""

    @app.get("/work/home")
    def home():
        return page(screen("TELLER HOME", """WELCOME TO COREONE TELLER.<br>
SELECT A FUNCTION FROM THE MENU AT LEFT.""") + notice_overlay())

    @app.route("/work/inquiry", methods=["GET", "POST"])
    def inquiry():
        msg = ""
        if request.method == "POST":
            mbr = request.form.get("f_0012", "").strip()
            if not mbr.isdigit() or not 5 <= len(mbr) <= 9:
                msg = "*** INVALID MEMBER NUMBER - MUST BE 5 TO 9 DIGITS ***"
            elif mbr not in MEMBERS:
                msg = f"*** NO RECORD FOUND FOR MEMBER {mbr} ***"
            elif MEMBERS[mbr].get("restricted"):
                msg = "*** ACCESS DENIED - RESTRICTED ACCOUNT. SUPERVISOR OVERRIDE REQUIRED ***"
            else:
                return redirect(f"/work/member/{mbr}")
        err = f"<font color=red><b>{msg}</b></font><br><br>" if msg else ""
        return page(screen("MEMBER INQUIRY", f"""{err}
<form method=post><table cellpadding=3>
<tr><td>{v.member_label}</td><td><input type=text name=f_0012 size=10 maxlength=12></td></tr>
<tr><td>Last Name</td><td><input type=text name=f_0013 size=20></td></tr>
<tr><td></td><td><input type=submit value="Inquire"> <input type=reset value="Clear"></td></tr>
</table></form>""") + notice_overlay())

    def member_or_404(mbr: str) -> dict:
        m = MEMBERS.get(mbr)
        if not m or m.get("restricted"):
            abort(404)
        return m

    def tabs(mbr: str) -> str:
        return (f'<a href="/work/member/{mbr}">Summary</a> | '
                f'<a href="/work/member/{mbr}/shares">{v.shares_tab}</a> | '
                f'<font color=gray>Loans</font> | '
                f'<a href="/work/member/{mbr}/open-share">Open Share</a><br><br>')

    @app.get("/work/member/<mbr>")
    def member_detail(mbr):
        m = member_or_404(mbr)
        return page(screen("MEMBER DETAIL", tabs(mbr) + f"""
<table border=1 cellpadding=3 bgcolor="#ffffff">
<tr><td>Member</td><td>{mbr}</td></tr>
<tr><td>Name</td><td>{m['name']}</td></tr>
<tr><td>SSN</td><td>{m['ssn']}</td></tr>
<tr><td>Birth Date</td><td>{m['dob']}</td></tr>
<tr><td>Address</td><td>{m['address']}</td></tr>
<tr><td>Phone</td><td>{m['phone']}</td></tr>
</table>""") + notice_overlay())

    @app.get("/work/member/<mbr>/shares")
    def shares(mbr):
        m = member_or_404(mbr)
        rows = "".join(
            f"<tr><td>{s['id']}</td><td>{s['type']}</td><td>{s['desc']}</td>"
            f"<td align=right>{s['balance']}</td><td align=right>{s['available']}</td></tr>"
            for s in m["shares"] + opened_shares.get(mbr, [])
        )
        return page(screen("MEMBER SHARES", tabs(mbr) + f"""
MEMBER {mbr} &nbsp; {m['name']}<br><br>
<table border=1 cellpadding=3 bgcolor="#ffffff">
<tr bgcolor="#cccccc"><td><b>Share</b></td><td><b>Type</b></td><td><b>Description</b></td>
<td><b>Balance</b></td><td><b>Available</b></td></tr>{rows}
</table>""") + notice_overlay())

    @app.route("/work/member/<mbr>/open-share", methods=["GET", "POST"])
    def open_share(mbr):
        member_or_404(mbr)
        msg = ""
        if request.method == "POST":
            stype = request.form.get("f_0201", "")
            dep = request.form.get("f_0202", "").replace(",", "").replace("$", "").strip()
            try:
                amount = float(dep)
            except ValueError:
                amount = -1
            if stype not in ("SAVINGS", "CLUB", "CERTIFICATE"):
                msg = "*** SELECT A SHARE TYPE ***"
            elif amount < 5:
                msg = "*** MINIMUM OPENING DEPOSIT IS 5.00 ***"
            else:
                session["pending"] = {"mbr": mbr, "type": stype, "amount": f"{amount:,.2f}",
                                      "nick": request.form.get("f_0203", "")[:20].upper()}
                return redirect(f"/work/member/{mbr}/open-share/verify")
        err = f"<font color=red><b>{msg}</b></font><br><br>" if msg else ""
        return page(screen("OPEN NEW SHARE", tabs(mbr) + f"""{err}
<form method=post><table cellpadding=3>
<tr><td>Share Type</td><td><select name=f_0201><option value="">--</option>
<option>SAVINGS</option><option>CLUB</option><option>CERTIFICATE</option></select></td></tr>
<tr><td>Opening Deposit</td><td><input type=text name=f_0202 size=10></td></tr>
<tr><td>Nickname</td><td><input type=text name=f_0203 size=20></td></tr>
<tr><td></td><td><input type=submit value="Continue"></td></tr>
</table></form>""") + notice_overlay())

    @app.route("/work/member/<mbr>/open-share/verify", methods=["GET", "POST"])
    def verify_share(mbr):
        pend = session.get("pending")
        if not pend or pend["mbr"] != mbr:
            return redirect(f"/work/member/{mbr}/open-share")
        if request.method == "POST":
            if request.form.get("back"):
                return redirect(f"/work/member/{mbr}/open-share")
            conf = f"CF-{random.randint(100000, 999999)}"
            sid = f"S{20 + len(opened_shares.get(mbr, []))}"
            opened_shares.setdefault(mbr, []).append({
                "id": sid, "type": pend["type"], "desc": pend["nick"] or f"NEW {pend['type']}",
                "balance": pend["amount"], "available": pend["amount"]})
            session.pop("pending")
            return page(screen("SHARE OPENED", f"""<b>SHARE {sid} OPENED SUCCESSFULLY.</b><br><br>
<table border=1 cellpadding=3 bgcolor="#ffffff">
<tr><td>Confirmation No</td><td>{conf}</td></tr>
<tr><td>Share</td><td>{sid}</td></tr><tr><td>Deposit</td><td>{pend['amount']}</td></tr>
</table>"""))
        return page(screen("VERIFY NEW SHARE", f"""PLEASE VERIFY THE FOLLOWING BEFORE POSTING.<br><br>
<table border=1 cellpadding=3 bgcolor="#ffffff">
<tr><td>Member</td><td>{mbr}</td></tr><tr><td>Share Type</td><td>{pend['type']}</td></tr>
<tr><td>Opening Deposit</td><td>{pend['amount']}</td></tr><tr><td>Nickname</td><td>{pend['nick']}</td></tr>
</table><br>
<form method=post><input type=submit value="Post"> <input type=submit name=back value="Back"></form>"""))

    @app.post("/work/reauth")
    def reauth():
        op = request.form.get("f_0001", "").upper()
        nxt = request.form.get("next", "/work/home")
        if OPERATORS.get(op) == request.form.get("f_0002"):
            session["op"] = op
            return redirect(nxt if nxt.startswith("/work/") else "/work/home")
        return timeout_page(nxt)

    @app.post("/work/verify-identity")
    def verify_identity():
        nxt = request.form.get("next", "/work/home")
        if request.form.get("cancel"):
            return redirect("/work/home")
        return redirect(nxt if nxt.startswith("/work/") else "/work/home")

    return app


def serve(variant: str = "acme", host: str = "127.0.0.1", port: int = 5055):
    """Start the app on a background thread; returns the werkzeug server."""
    from werkzeug.serving import make_server

    import logging

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server(host, port, create_app(variant), threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True, name=f"mockbank-{variant}")
    t.start()
    return server


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="acme", choices=sorted(VARIANTS))
    ap.add_argument("--port", type=int, default=5055)
    args = ap.parse_args()
    create_app(args.variant).run(host="127.0.0.1", port=args.port, threaded=True)
