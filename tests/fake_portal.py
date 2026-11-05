"""A fake eHospital, served entirely inside Playwright (no network), for offline tests."""
from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import parse_qs, urlsplit
import uuid

ORIGIN = "https://ehospital.test"
CAPTCHA = "AB12CD"
OTP = "123456"
USER, PASSWORD = "intern", "secret-pass"

PATIENTS = {
    "20260000001": [
        {"test": "Complete Blood Count", "sample": "S1001", "date": "20/10/2026", "status": "Released"},
        {"test": "Liver Function Test", "sample": "S1002", "date": "21/10/2026", "status": "Released"},
        {"test": "Kidney Function Test", "sample": "S1003", "date": "22/10/2026", "status": "Released"},
    ],
}

RESULT_LINES = {
    "S1001": ["Haemoglobin 12.5 g/dL 13.0 - 17.0", "Total Leucocyte Count 8.2 x10^3/uL 4.0 - 11.0"],
    "S1002": ["Total Bilirubin 1.1 mg/dL 0.2 - 1.2", "SGPT 42 U/L 7 - 56"],
    "S1003": ["Creatinine 1.1 mg/dL 0.7 - 1.3", "Sodium 138 mmol/L 135 - 145"],
}


def make_pdf(lines: list[str]) -> bytes:
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 11 Tf 50 790 Td 15 TL " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


APP_JS = r"""
window.api = async (method, path, body) => {
  const r = await fetch(path, {method, headers: {'Content-Type': 'application/json',
             'Authorization': localStorage.getItem('token') || ''}, body: body ? JSON.stringify(body) : undefined});
  let data = null; try { data = await r.json(); } catch (e) {}
  return {status: r.status, data};
};
window.requireAuth = async () => {
  const me = await api('GET', '/api/me');
  if (me.status !== 200) { location.href = '/login'; return null; }
  return me.data;
};
window.wireLogout = () => {
  const el = document.getElementById('logout');
  if (el) el.onclick = async (e) => { e.preventDefault(); await api('POST', '/api/authentication/v1/logout');
                                      localStorage.clear(); location.href = '/'; };
};
"""

PAGES = {
    "/": """<h1>eHospital (fake)</h1><a href="/login">Login</a>""",

    "/login": """
<form id="f" onsubmit="return false">
  <input formcontrolname="username" id="u"> <input formcontrolname="password" type="password" id="p">
  <img class="img-control" id="cap" src="" alt="captcha" style="width:120px;height:40px">
  <i class="fa-refresh" id="refresh" style="display:inline-block;width:20px;height:20px;cursor:pointer">R</i>
  <input formcontrolname="captchaValue" maxlength="6" id="capv">
  <button type="button" id="send">Send OTP</button>
  <div id="alert" class="alert-danger" style="display:none"></div>
</form>
<div id="otpbox" style="display:none"><input formcontrolname="otp" id="otp"><button type="button" id="verify">Verify</button>
  <div id="otperr" class="alert-danger" style="display:none">Invalid OTP</div></div>
<div class="swal2-popup" id="swal" style="display:none"><div class="swal2-html-container">User already logged IN. Do you want to login again?</div>
  <button class="swal2-confirm" id="yes">Yes</button></div>
<script>
let confirmFlag = false;
const $ = id => document.getElementById(id);
async function newCaptcha() { const r = await api('GET', '/api/authentication/v1/captcha_image'); $('cap').src = r.data.src; $('capv').value = ''; }
$('refresh').onclick = newCaptcha;
$('send').onclick = async () => {
  $('alert').style.display = 'none';
  const r = await api('POST', '/api/authentication/v1/login/web_two_factor',
                      {u: $('u').value, p: $('p').value, c: $('capv').value, confirm: confirmFlag});
  const res = r.data.result;
  if (res === 'captcha_bad') { $('alert').innerText = 'Entered captcha is invalid'; $('alert').style.display = 'block'; await newCaptcha(); }
  else if (res === 'cred_bad') { $('alert').innerText = 'Invalid username or password'; $('alert').style.display = 'block'; }
  else if (res === 'already') { $('swal').style.display = 'block'; }
  else if (res === 'otp_sent') { $('f').style.display = 'none'; $('otpbox').style.display = 'block'; }
};
$('yes').onclick = async () => { $('swal').style.display = 'none'; confirmFlag = true; await newCaptcha(); };
$('verify').onclick = async () => {
  const r = await api('POST', '/api/authentication/v1/login/verfiy/web_two_factor', {otp: $('otp').value});
  if (r.status === 200) { localStorage.setItem('token', r.data.token); location.href = '/home'; }
  else { $('otperr').style.display = 'block'; }
};
newCaptcha();
</script>""",

    "/home": """
<div id="hpr" style="display:none">HPR registration is pending. <button id="skip">Skip</button></div>
<div id="main" style="display:none"><a href="/lis/" target="_blank" id="lis">Lab Information System</a>
  <a href="#" id="logout">Logout</a></div>
<script>
(async () => { const me = await requireAuth(); if (!me) return; wireLogout();
  const $ = id => document.getElementById(id);
  if (me.hpr_pending) { $('hpr').style.display = 'block';
    $('skip').onclick = async () => { await api('POST', '/api/user/hpr_skip'); $('hpr').style.display = 'none'; $('main').style.display = 'block'; };
  } else { $('main').style.display = 'block'; }
})();
</script>""",

    "/lis/": """
<div id="nav" style="display:none">
  <div class="iocn-link" id="menu" style="cursor:pointer"><span>LIS Transaction</span></div>
  <div id="sub" style="display:none"><a href="/lis/report" id="rep">Report</a></div>
</div>
<a href="#" id="logout">Logout</a>
<script>
(async () => { const me = await requireAuth(); if (!me) return; wireLogout();
  const $ = id => document.getElementById(id);
  if (!me.broken) $('nav').style.display = 'block';
  let clicks = 0; $('menu').onclick = () => { clicks++; if (clicks >= 2) $('sub').style.display = 'block'; };
})();
</script>""",

    "/lis/report": """
<div><span>LIS Transaction</span> <a href="#" id="logout">Logout</a></div>
<form onsubmit="return false" id="form" style="display:none">
  <select name="lab_centre_id" id="lab"><option value="">-- Select --</option><option value="all">All Laboratories</option>
          <option value="bio">Biochemistry Lab</option></select>
  <input type="radio" id="regisNo" name="by"><label for="regisNo">Registration No</label>
  <input name="patient_uhid" id="uhid">
  <input type="radio" id="date" name="ds"><label for="date">Date Search</label>
  <span id="dates"></span>
  <button type="submit" id="search" class="btn-primary-r">Search</button>
</form>
<h5 id="nodata">Data not Found</h5>
<div id="wait" style="display:none">Please Wait...</div>
<table id="t" style="display:none"><thead><tr><th>Select</th><th>Test Name</th><th>Sample No</th><th>Collection Date</th><th>Status</th></tr></thead><tbody id="tb"></tbody></table>
<button type="button" id="print" class="btn-primary-r" style="display:none">Print</button>
<button type="button" id="save" style="display:none">Save Remarks</button>
<script>
(async () => { const me = await requireAuth(); if (!me) return; wireLogout();
  const $ = id => document.getElementById(id);
  $('form').style.display = 'block';
  $('date').onchange = () => { $('dates').innerHTML = '<input name="FromDate" id="fd"> <input name="ToDate" id="td">'; };
  $('search').onclick = async () => {
    $('wait').style.display = 'block'; $('t').style.display = 'none'; $('print').style.display = 'none';
    const r = await api('POST', '/api/lis/report/search', {uhid: $('uhid').value, lab: $('lab').value,
                         from: ($('fd') || {}).value, to: ($('td') || {}).value});
    $('wait').style.display = 'none';
    if (r.status === 401) { localStorage.clear(); location.href = '/login'; return; }
    const rows = r.data.rows; $('tb').innerHTML = '';
    if (!rows.length) { $('nodata').style.display = 'block'; return; }
    $('nodata').style.display = 'none';
    for (const row of rows) {
      $('tb').insertAdjacentHTML('beforeend', `<tr><td><input type="checkbox" name="piscine" id="piscine"></td>
        <td>${row.test}</td><td>${row.sample}</td><td>${row.date}</td><td>${row.status}</td></tr>`);
    }
    $('t').style.display = 'table'; $('print').style.display = 'inline'; $('save').style.display = 'inline';
  };
  $('print').onclick = async () => {
    const picked = [...document.querySelectorAll('#tb tr')].filter(tr => tr.querySelector('input').checked)
                     .map(tr => tr.children[2].innerText.trim());
    const r = await fetch('/api/lis/report/pdf?uhid=' + encodeURIComponent($('uhid').value) + '&samples=' + picked.join(','),
                          {headers: {Authorization: localStorage.getItem('token') || ''}});
    const url = URL.createObjectURL(await r.blob());
    window.open(url, '_blank');
  };
  $('save').onclick = async () => { const r = await api('POST', '/api/lis/remarks/save', {note: 'x'}); window.__saveStatus = r.status; };
})();
</script>""",
}


class FakeHospital:
    def __init__(self, *, force_already_once: bool = False, broken_logins: int = 0,
                 expire_on_first_search: bool = False, hpr: bool = True, search_delay: float = 0.7):
        self.force_already_once = force_already_once
        self.broken_logins = broken_logins
        self.expire_on_first_search = expire_on_first_search
        self.hpr = hpr
        self.search_delay = search_delay
        self.tokens: dict[str, dict] = {}
        self.captcha_serial = 0
        self.logins = 0
        self.logouts = 0
        self.search_calls = 0
        self.writes_received: list[str] = []
        self._already_shown = False
        self._otp_pending = False

    async def install(self, context) -> None:
        await context.route(f"{ORIGIN}/**", self._handle)

    async def _json(self, route, data, status=200):
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

    def _session(self, route):
        return self.tokens.get(route.request.headers.get("authorization") or "")

    def _page(self, path: str) -> str | None:
        body = PAGES.get(path)
        if body is None:
            return None
        return f"<!doctype html><html><head><meta charset='utf-8'><title>eHospital</title><script>{APP_JS}</script></head><body>{body}</body></html>"

    async def _handle(self, route):
        request = route.request
        parts = urlsplit(request.url)
        path, method = parts.path, request.method.upper()
        if not path.startswith("/api/"):
            html = self._page(path)
            if html is None:
                await route.fulfill(status=404, body="not found")
            else:
                await route.fulfill(status=200, content_type="text/html", body=html)
            return
        body = {}
        if method == "POST":
            try:
                body = request.post_data_json or {}
            except Exception:
                body = {}

        if path == "/api/authentication/v1/captcha_image":
            self.captcha_serial += 1
            src = "data:image/png;base64," + base64.b64encode(f"captcha-{self.captcha_serial}".encode()).decode()
            return await self._json(route, {"src": src})

        if path == "/api/authentication/v1/login/web_two_factor":
            if body.get("u") != USER or body.get("p") != PASSWORD:
                return await self._json(route, {"result": "cred_bad"})
            if body.get("c") != CAPTCHA:
                return await self._json(route, {"result": "captcha_bad"})
            if not body.get("confirm") and (self.tokens or (self.force_already_once and not self._already_shown)):
                self._already_shown = True
                return await self._json(route, {"result": "already"})
            if body.get("confirm"):
                self.tokens.clear()
            self._otp_pending = True
            return await self._json(route, {"result": "otp_sent"})

        if path == "/api/authentication/v1/login/verfiy/web_two_factor":
            if not self._otp_pending or body.get("otp") != OTP:
                return await self._json(route, {"error": "invalid"}, 400)
            self._otp_pending = False
            token = uuid.uuid4().hex
            self.tokens[token] = {"broken": self.logins < self.broken_logins, "hpr_pending": self.hpr}
            self.logins += 1
            return await self._json(route, {"token": token})

        if path == "/api/authentication/v1/logout":
            self.tokens.pop(request.headers.get("authorization") or "", None)
            self.logouts += 1
            return await self._json(route, {"ok": True})

        session = self._session(route)
        if path == "/api/me":
            return await self._json(route, session or {}, 200 if session else 401)
        if session is None:
            return await self._json(route, {"error": "unauthorized"}, 401)

        if path == "/api/user/hpr_skip":
            session["hpr_pending"] = False
            return await self._json(route, {"ok": True})

        if path == "/api/lis/report/search":
            self.search_calls += 1
            if self.expire_on_first_search and self.search_calls == 1:
                self.tokens.clear()
                return await self._json(route, {"error": "session expired"}, 401)
            await asyncio.sleep(self.search_delay)
            return await self._json(route, {"rows": PATIENTS.get(body.get("uhid", ""), [])})

        if path == "/api/lis/report/pdf":
            query = parse_qs(parts.query)
            uhid = query.get("uhid", [""])[0]
            samples = [s for s in query.get("samples", [""])[0].split(",") if s]
            lines = [f"Patient UHID: {uhid}", "Laboratory Results"]
            for sample in samples:
                lines += [f"Sample {sample}"] + RESULT_LINES.get(sample, [])
            return await route.fulfill(status=200, content_type="application/pdf", body=make_pdf(lines))

        if path == "/api/lis/remarks/save":
            self.writes_received.append(path)
            return await self._json(route, {"ok": True})

        await route.fulfill(status=404, body="no such api")
