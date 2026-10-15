#!/usr/bin/env python3
"""Local stand-ins for the five NCAP portals.

This exists purely so the scrapers can be exercised end-to-end (real Chromium,
real form fill, real parsing) without hammering the live government sites, and
without needing internet access at all.

Run it with::

    python scripts/mock_portals.py --port 5099

Then point a source at ``http://127.0.0.1:5099/qc/check-violations?mode=violations``
and use Admin -> Sources -> Probe, or run the integration test suite.

Each portal accepts a ``mode`` query parameter:

* ``violations`` (default) - return two violation records
* ``clean``               - return the "no violations" state
* ``blocked``             - return a Cloudflare-style challenge page
"""
from __future__ import annotations

import argparse

from flask import Flask, request

PRIVACY_STYLE = """
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 900px; }
    .modal { position: fixed; inset: 0; background: rgba(15,23,32,.75); display: flex;
             align-items: center; justify-content: center; }
    .modal-card { background: #fff; padding: 1.5rem; border-radius: .75rem; width: 420px; }
    label { display: block; margin: .5rem 0 .25rem; font-size: .9rem; }
    input, select { padding: .45rem; width: 100%; max-width: 320px; }
    button { padding: .5rem 1rem; margin-top: .75rem; cursor: pointer; }
    table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
    th, td { border: 1px solid #d7dde3; padding: .4rem .6rem; text-align: left; font-size: .9rem; }
    th { background: #f1f5f9; }
    .empty { border: 1px dashed #16a34a; padding: 1rem; border-radius: .5rem; margin-top: 1rem; }
  </style>
"""


def _plate_and_mv():
    plate = (request.form.get("plate_number") or request.args.get("plate_number")
             or request.form.get("plate") or "ABC1234")
    mv = (request.form.get("mv_file_number") or request.args.get("mv_file_number")
          or request.form.get("mv") or "1234567890")
    return plate, mv


def _mode() -> str:
    return request.args.get("mode", request.form.get("mode", "violations"))


def _portal_page(title: str, mode: str, action: str) -> str:
    """The GET page: privacy gate, then the search form."""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>{PRIVACY_STYLE}</head>
<body>
  <h1>Check for Violations &mdash; {title}</h1>
  <div class="modal" id="privacy">
    <div class="modal-card">
      <h2>Privacy and Terms of Use</h2>
      <p>This website uses cookies and collects limited personal data.</p>
      <button id="agree" onclick="document.cookie='ncap_consent=1; path=/';
        document.getElementById('privacy').style.display='none';
        document.getElementById('form').style.display='block';">I Agree &amp; Continue</button>
    </div>
  </div>
  <div id="form" style="display:none">
    <div class="tabs">
      <button type="button" class="tab active">Plate + MV File No.</button>
      <button type="button" class="tab">Plate + NOV No.</button>
    </div>
    <form method="post" action="{action}">
      <input type="hidden" name="mode" value="{mode}">
      <label for="plate_number">Plate Number</label>
      <input id="plate_number" name="plate_number" placeholder="Plate Number" autocomplete="off">
      <label for="mv_file_number">MV File Number</label>
      <input id="mv_file_number" name="mv_file_number" placeholder="MV File Number" autocomplete="off">
      <div class="cf-turnstile" data-sitekey="0xTESTSITEKEY"></div>
      <input type="hidden" name="cf-turnstile-response" value="mock-token">
      <button type="submit" id="search">Search</button>
    </form>
  </div>
</body></html>"""


def _results_table(title: str, plate: str, mode: str) -> str:
    if mode == "clean":
        body = ('<div class="empty"><h3>No violations found</h3>'
                '<p>This vehicle has no recorded violation.</p></div>')
    else:
        body = f"""
    <table class="violations">
      <thead><tr>
        <th>Plate Number</th><th>Ticket No.</th><th>Violation Date</th><th>Time</th>
        <th>Location</th><th>Violation</th><th>Amount</th><th>Status</th>
      </tr></thead>
      <tbody>
        <tr>
          <td>{plate}</td><td>MOCK-0001</td><td>2026-09-12</td><td>08:32 AM</td>
          <td>Commonwealth Ave. cor. Don Antonio</td><td>Disregarding Traffic Sign</td>
          <td>2,000.00</td><td>Unpaid</td>
        </tr>
        <tr>
          <td>{plate}</td><td>MOCK-0002</td><td>2026-09-20</td><td>17:45</td>
          <td>Quezon Ave. cor. EDSA</td><td>Illegal Parking</td>
          <td>1,500.00</td><td>Unpaid</td>
        </tr>
      </tbody>
    </table>"""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Results &mdash; {title}</title>{PRIVACY_STYLE}</head>
<body>
  <h1>Results &mdash; {title}</h1>
  <p>Plate <strong>{plate}</strong></p>
  {body}
</body></html>"""


CHALLENGE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Just a moment...</title></head>
<body>
  <div>Checking your browser before accessing the portal.</div>
  <div>Please enable JavaScript and cookies to continue.</div>
  <form id="challenge-form">
    <div class="cf-turnstile" data-sitekey="0xTESTSITEKEY"></div>
    <input type="hidden" name="cf-turnstile-response" value="">
  </form>
</body></html>"""


def create_mock_app() -> Flask:
    app = Flask(__name__)

    def portal(title: str):
        def view():
            mode = _mode()
            if mode == "blocked":
                return CHALLENGE_PAGE
            if request.method == "POST":
                plate, _mv = _plate_and_mv()
                return _results_table(title, plate, mode)
            action = request.path + f"?mode={mode}"
            return _portal_page(title, mode, action)

        return view

    for prefix, title in (("qc", "Quezon City NCAP"),
                          ("paranaque", "Parañaque City NCAP"),
                          ("sanjuan", "San Juan City NCAP")):
        app.add_url_rule(f"/{prefix}/check-violations", endpoint=f"{prefix}_portal",
                         view_func=portal(title), methods=["GET", "POST"])

    @app.route("/manila/nocontact", methods=["GET", "POST"])
    def manila():
        mode = _mode()
        if mode == "blocked":
            return CHALLENGE_PAGE
        if request.method == "POST":
            plate, _mv = _plate_and_mv()
            return _results_table("Manila NCAP", plate, mode)
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Manila NCAP</title>{PRIVACY_STYLE}</head>
<body>
  <h1>Notice of Violations Inquiry</h1>
  <form method="post" action="/manila/nocontact?mode={mode}">
    <input type="hidden" name="mode" value="{mode}">
    <label for="plate_number">Plate Number</label>
    <input id="plate_number" name="plate_number" placeholder="Plate Number">
    <label for="lookup_by">Look Up By</label>
    <select id="lookup_by" name="lookup_by">
      <option value="">-Select-</option>
      <option value="plate">Plate Number</option>
      <option value="mv">MV File Number</option>
    </select>
    <div class="g-recaptcha" data-sitekey="6LeTEST"></div>
    <button type="submit" id="inquire">Inquire</button>
  </form>
</body></html>"""

    @app.route("/", methods=["GET", "POST"])
    @app.route("/mmda/", methods=["GET", "POST"])
    def mmda():
        mode = _mode()
        if mode == "blocked":
            return CHALLENGE_PAGE
        if request.method == "POST":
            plate, _mv = _plate_and_mv()
            return _results_table("MMDA May Huli Ka", plate, mode)
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>May Huli Ka</title>{PRIVACY_STYLE}</head>
<body>
  <h1>May Huli Ka</h1>
  <form method="post" action="/mmda/?mode={mode}">
    <input type="hidden" name="mode" value="{mode}">
    <label for="plate_number">Plate / Conduction Sticker No.</label>
    <input id="plate_number" name="plate_number" placeholder="Plate Number">
    <label for="mv_file_number">MV File Number</label>
    <input id="mv_file_number" name="mv_file_number" placeholder="MV File Number">
    <button type="submit" id="check">CHECK</button>
  </form>
</body></html>"""

    @app.route("/api/violations/search", methods=["GET", "POST"])
    def api_search():
        """JSON endpoint used to exercise the HTTP engine."""
        from flask import jsonify

        mode = _mode()
        plate, _mv = _plate_and_mv()
        if mode == "blocked":
            return CHALLENGE_PAGE, 403
        if mode == "clean":
            return jsonify({"status": "success", "data": {"violations": []}})
        return jsonify({
            "status": "success",
            "data": {"violations": [
                {"plate_number": plate, "ticket_no": "API-1", "nov_no": "NOV-1",
                 "violation_date": "2026-09-01", "violation_time": "10:15:00",
                 "location": "C-5 Road", "violation": "Over speeding",
                 "amount": "3000.00", "status": "Unpaid", "due_date": "2026-10-01"},
                {"plate_number": plate, "ticket_no": "API-2", "nov_no": "NOV-2",
                 "violation_date": "2026-09-05", "violation_time": "19:40:00",
                 "location": "Katipunan Ave.", "violation": "Illegal U-turn",
                 "amount": "1000", "status": "Paid", "due_date": "2026-10-05"},
            ]},
        })

    @app.route("/health")
    def health():
        return {"ok": True}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5099)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    create_mock_app().run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
