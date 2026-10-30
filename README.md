# Pipeflame
An application agnostic flamegraph viewer built in python. With any application or programming language, generate detailed customizeable flamegraphs by sending data from the local environment to a readable pipe.

### Getting speedscope

The flamegraph viewer is [speedscope](https://github.com/jlfwong/speedscope), served from `static/speedscope`. It isn't checked in, so download it once after cloning. Run these from the project folder.

macOS
```bash
curl -fsSLO https://registry.npmjs.org/speedscope/-/speedscope-1.22.2.tgz
tar xzf speedscope-1.22.2.tgz
mkdir -p static/speedscope
cp -R package/dist/release/ static/speedscope/
cp package/LICENSE static/speedscope/
rm -rf package speedscope-1.22.2.tgz
```

Windows (PowerShell)
```powershell
curl.exe -fsSLO https://registry.npmjs.org/speedscope/-/speedscope-1.22.2.tgz
tar xzf speedscope-1.22.2.tgz
New-Item -ItemType Directory -Force static\speedscope | Out-Null
Copy-Item -Recurse package\dist\release\* static\speedscope\
Copy-Item package\LICENSE static\speedscope\
Remove-Item -Recurse -Force package, speedscope-1.22.2.tgz
```

`static/speedscope/index.html` should now exist. To upgrade, delete `static/speedscope` and rerun with a newer version number.

### Setting up environment

Setting the export location
```bash
python -m venv .venv
pip install -r requirements.txt
export PIPEFLAME_PIPE='path/to/pipe-or-sock'
```

(optional) Setting security parameters to protect this data on live deployments
```bash
export PIPEFLAME_PASSCODE=''
```

### Running

Start the app from the project folder with the virtual environment active (`source .venv/bin/activate` on macOS, `.venv\Scripts\activate` on Windows):
```bash
python app.py
```

Then open http://localhost:3302. The flamegraph fills in as clients send calls; press Refresh to reload it, or pick an Auto-refresh interval (15 seconds, 30 seconds or 1 minute) in the toolbar. Each browser remembers its own choice, and each reload resets the viewer's zoom.

| Argument | Description |
| --- | --- |
| `--pipe PATH` | Pipe to listen on: a named pipe on Windows (e.g. `\\.\pipe\pipeflame`), a socket path on macOS (e.g. `/tmp/pipeflame.sock`). Overrides `PIPEFLAME_PIPE`. One of the two is required. |
| `--window SECONDS` | Keep only calls from the last `SECONDS`; a top-level call is dropped, with everything under it, once it ended longer ago than that. Defaults to `1800` (30 minutes). Lower it on busy apps to keep memory and reload time down. |
| `--pending` | When a client disconnects partway through a line (no trailing newline), process that last line anyway. By default it is dropped. |

```bash
python app.py --pipe /tmp/pipeflame.sock --pending
```

On macOS, socket paths must be under 104 characters.

### Sending calls

Clients connect to the pipe and write one tab-separated line per event: an `O` (open) line when a call starts and a `C` (close) line when it returns. Calls nest per `pid`: a `C` line closes the most recent open call with the same `object` and `function`. Any calls still open inside it are closed at the same time, so a lost `C` line doesn't leave the stack stuck. A `C` line that matches no open call is ignored.

```text
O<TAB>pid<TAB>tenant_id<TAB>object<TAB>function<TAB>message
C<TAB>pid<TAB>object<TAB>function
```

For example, with tabs shown as `→`:
```text
O→4101→101→Db→query→SELECT * FROM orders
C→4101→Db→query
```

| Field | Description |
| --- | --- |
| `pid` | Process or worker id; each pid gets its own call stack |
| `tenant_id` | Integer used by the Tenant filter (`O` lines only) |
| `object`, `function` | Shown as `object.function` on the flamegraph |
| `message` | Optional detail such as a SQL query, shown in speedscope's detail panel (`O` lines only). It may contain tabs but not newlines, since a newline ends the line. |

Timing is taken when pipeflame receives each line. Lines with an unknown event or the wrong number of fields are logged and dropped.

To check that it's working, send a call from Python while the app is running:
```python
import socket

sock = socket.socket(socket.AF_UNIX)  # on Windows: pipe = open(r"\\.\pipe\pipeflame", "w")
sock.connect("/tmp/pipeflame.sock")
sock.sendall(b"O\t1\t1\tDemo\trun\n"
             b"C\t1\tDemo\trun\n")
sock.close()
```

