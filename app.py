import argparse
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from frame_recorder import FrameRecorder
from pipe_receiver import PIPE_ENV_VAR, create_receiver

log = logging.getLogger(__name__)

# without a passcode, every route is open
PASSCODE = os.environ.get("PIPEFLAME_PASSCODE")

app = Flask(__name__)
if not PASSCODE:
    log.warning("PIPEFLAME_PASSCODE is not set: pipeflame is running without a login")
# without PIPEFLAME_SECRET_KEY, a restart logs everyone out
app.secret_key = os.environ.get("PIPEFLAME_SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
recorder = FrameRecorder()
# each pipe client writes on its own thread while requests read, so all recorder access holds this
recorder_lock = threading.Lock()
# seconds between profile reloads in the browser; -1 means no refresh
app.config["AUTOREFRESH"] = -1

def handle_line(line: str) -> None:
    """Apply one tab-separated line from a pipe client:
    O <pid> <tenant_id> <object> <function> [<message>]
    C <pid> <object> <function>
    The message is everything after the fifth tab, so it may contain tabs itself.
    Malformed lines are logged and dropped."""
    if not line.strip():
        return
    try:
        kind, _, rest = line.partition("\t")
        if kind == "O":
            pid, tenant_id, object, function, *message = rest.split("\t", 4)
            with recorder_lock:
                recorder.open_call(object, function, message[0] if message else None, int(pid), int(tenant_id))
        elif kind == "C":
            pid, object, function = rest.split("\t")
            with recorder_lock:
                recorder.close_call(object, function, int(pid))
        else:
            raise ValueError(f"unknown event {kind!r}")
    except ValueError as e:  # also raised when a line has the wrong number of fields
        log.warning("dropped line %r: %s", line[:200], e)

# stored in the session so changing the passcode logs everyone out
PASSCODE_ID = hashlib.sha256(PASSCODE.encode()).hexdigest() if PASSCODE else None

@app.before_request
def require_login():
    if not PASSCODE or request.endpoint == "login" or session.get("auth") == PASSCODE_ID:
        return
    if request.path.endswith(".json"):
        return jsonify(error="login required"), 401
    return redirect(url_for("login"))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not PASSCODE:
        return redirect(url_for("route"))
    if request.method == 'POST':
        if hmac.compare_digest(request.form.get("passkey", "").encode(), PASSCODE.encode()):
            session.clear()
            session["auth"] = PASSCODE_ID
            return redirect(url_for("route"))
        time.sleep(1)  # slow down guessing
        return redirect(url_for("login", error=1))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for("login" if PASSCODE else "route"))

@app.route('/')
def route():
    # main.html hosts the controls and embeds speedscope from static/speedscope
    return render_template('main.html', autorefresh=app.config["AUTOREFRESH"])

@app.route('/profile.json')
def profile():
    tenant_id = request.args.get('tenant_id', type=int)
    with recorder_lock:
        return jsonify(recorder.to_speedscope(tenant_id=tenant_id))

@app.route('/tenants.json')
def tenants():
    with recorder_lock:
        return jsonify(recorder.tenant_ids())

def main() -> None:
    parser = argparse.ArgumentParser(description="Serve live flamegraphs of calls received over a pipe.")
    parser.add_argument("--pipe", help=f"pipe name on Windows, socket path elsewhere; overrides ${PIPE_ENV_VAR}")
    parser.add_argument("--autorefresh", type=float, default=-1, metavar="SECONDS",
                        help="reload the profile in the browser every SECONDS (default -1: no refresh)")
    args = parser.parse_args()
    if args.autorefresh != -1 and args.autorefresh <= 0:
        parser.error("--autorefresh must be positive, or -1 for no refresh")
    app.config["AUTOREFRESH"] = args.autorefresh

    try:
        receiver = create_receiver(handle_line, args.pipe)
    except OSError as e:
        parser.error(str(e))
    receiver.start()
    log.warning("listening on %s", receiver.pipe_name)
    try:
        app.run(port=3302)
    finally:
        if hasattr(receiver, "close"):
            receiver.close()

if __name__ == "__main__":
    main()
