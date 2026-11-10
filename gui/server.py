#!/usr/bin/env python3
"""Kleine web-GUI voor de aangepaste Stockfish (pawn7-regels).

Start:  python3 gui/server.py [pad/naar/stockfish] [--port 8000]
Open daarna http://127.0.0.1:8000 (opent automatisch).

Alleen standaardbibliotheek. De engine doet alle regels (legale zetten,
promotie op de 7de rank, schaak/mat); de GUI praat er via UCI mee.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENGINE = os.path.join(HERE, "..", "src", "stockfish")
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class Busy(Exception):
    pass


class EngineError(Exception):
    pass


class Engine:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.lock = threading.RLock()
        self.cond = threading.Condition()
        self.saved = {}  # optienaam -> waarde (voor herstart)
        self.options = []
        self.searching = False
        self.sid = 0
        self.mode = None
        self.infos = {}
        self.best = None
        self._spawn()
        self.options = self._read_options()

    # ---------------------------------------------------------------- proces
    def _spawn(self):
        self.p = subprocess.Popen(
            [self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=os.path.dirname(self.path))
        self.q = queue.Queue()
        threading.Thread(target=self._reader, args=(self.p, self.q), daemon=True).start()

    def _reader(self, proc, q):
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if proc is self.p and self.searching and (
                    line.startswith("info ") or line.startswith("bestmove")):
                self._search_line(line)
            else:
                q.put(line)
        q.put(None)  # EOF
        if proc is self.p:
            with self.cond:
                if self.searching:
                    self.searching = False
                    self.best = "(none)"
                self.cond.notify_all()

    def _search_line(self, line):
        if line.startswith("bestmove"):
            with self.cond:
                parts = line.split()
                self.best = parts[1] if len(parts) > 1 else "(none)"
                self.searching = False
                self.cond.notify_all()
            return
        t = line.split()
        if "pv" not in t or "score" not in t or "depth" not in t:
            return
        try:
            g = lambda k: t[t.index(k) + 1]
            si = t.index("score")
            info = {
                "depth": int(g("depth")),
                "seldepth": int(g("seldepth")) if "seldepth" in t else None,
                "multipv": int(g("multipv")) if "multipv" in t else 1,
                "scoreType": t[si + 1],
                "score": int(t[si + 2]),
                "nodes": int(g("nodes")) if "nodes" in t else 0,
                "nps": int(g("nps")) if "nps" in t else 0,
                "time": int(g("time")) if "time" in t else 0,
                "pv": t[t.index("pv") + 1:],
            }
        except (ValueError, IndexError):
            return
        self.infos[info["multipv"]] = info

    def _send(self, text):
        try:
            self.p.stdin.write(text + "\n")
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            raise EngineError("engine is gestopt")

    def _restart(self):
        try:
            self.p.kill()
        except Exception:
            pass
        self._spawn()
        self._sync(["uci"], "uciok")
        for name, val in self.saved.items():
            self._set(name, val)

    def _sync(self, cmds, until="readyok", timeout=20):
        """Stuur commando's en verzamel regels tot `until`."""
        while True:  # oude regels weggooien
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        for c in cmds:
            self._send(c)
        if until == "readyok":
            self._send("isready")
        lines = []
        while True:
            try:
                line = self.q.get(timeout=timeout)
            except queue.Empty:
                raise EngineError("time-out van de engine")
            if line is None:
                raise EngineError("engine is gestopt")
            lines.append(line)
            if "CRITICAL ERROR" in line:
                raise EngineError(line.split("Reason:")[-1].strip())
            if line.strip() == until:
                return lines

    def _read_options(self):
        lines = self._sync(["uci"], "uciok")
        opts = []
        for line in lines:
            m = re.match(r"option name (.+?) type (\w+)(.*)$", line)
            if not m:
                continue
            name, typ, rest = m.groups()
            o = {"name": name, "type": typ, "default": "", "min": None, "max": None, "vars": []}
            d = re.search(r" default (.*?)(?= min | max | var |$)", rest)
            if d:
                o["default"] = "" if d.group(1) == "<empty>" else d.group(1)
            for k in ("min", "max"):
                mm = re.search(r" %s (-?\d+)" % k, rest)
                if mm:
                    o[k] = int(mm.group(1))
            o["vars"] = re.findall(r" var (\S+)", rest)
            opts.append(o)
        return opts

    def _set(self, name, value):
        opt = next((o for o in self.options if o["name"] == name), None)
        if opt and opt["type"] == "button":
            self._sync(["setoption name %s" % name])
            return
        v = str(value)
        if v == "":
            v = "<empty>"
        self._sync(["setoption name %s value %s" % (name, v)], timeout=60)

    # ------------------------------------------------------------- publieke API
    def _check_free(self):
        if self.searching:
            raise Busy("engine is bezig; druk eerst op Stop")

    def set_option(self, name, value):
        with self.lock:
            self._check_free()
            self._set(name, value)
            opt = next((o for o in self.options if o["name"] == name), None)
            if opt and opt["type"] != "button":
                self.saved[name] = value

    @staticmethod
    def _prevalidate(fen):
        parts = fen.split()
        if len(parts) < 2:
            raise EngineError("ongeldige FEN")
        rows = parts[0].split("/")
        if len(rows) != 8:
            raise EngineError("ongeldige FEN (geen 8 rijen)")
        if parts[0].count("K") != 1 or parts[0].count("k") != 1:
            raise EngineError("elk kleur heeft precies één koning nodig")
        if re.search(r"[Pp]", rows[0] + rows[7]):
            raise EngineError("pionnen mogen niet op rij 1 of 8 staan")
        if parts[0].count("P") > 8 or parts[0].count("p") > 8:
            raise EngineError("te veel pionnen")

    def state(self, fen, move=None):
        """Geef genormaliseerde FEN, legale zetten en schaak-status."""
        with self.lock:
            self._check_free()
            self._prevalidate(fen)
            pos = "position fen %s" % fen + (" moves %s" % move if move else "")
            try:
                lines = self._sync([pos, "d", "go perft 1"])
            except EngineError:
                self._restart()  # engine sluit zich bij een ongeldige stelling
                raise
        new_fen = fen
        checkers = False
        moves = []
        for line in lines:
            if line.startswith("Fen:"):
                new_fen = line[4:].strip()
            elif line.startswith("Checkers:"):
                checkers = bool(line[9:].strip())
            else:
                m = re.match(r"^([a-h][1-8][a-h][1-8][nbrq]?): \d+$", line)
                if m:
                    moves.append(m.group(1))
        return {"fen": new_fen, "moves": moves, "check": checkers}

    def go(self, fen, mode, depth, movetime):
        with self.lock:
            if self.searching:
                self._stop_locked()
            self._prevalidate(fen)
            self.sid += 1
            self.mode = mode
            self.infos = {}
            self.best = None
            self._sync(["position fen %s" % fen])
            if depth and depth > 0:
                cmd = "go depth %d" % depth
            elif mode == "play":
                cmd = "go movetime %d" % max(50, movetime or 1000)
            else:
                cmd = "go infinite"
            self.searching = True
            try:
                self._send(cmd)
            except EngineError:
                self.searching = False
                raise
            return self.sid

    def _stop_locked(self):
        if not self.searching:
            return
        self._send("stop")
        with self.cond:
            self.cond.wait_for(lambda: not self.searching, timeout=10)

    def stop(self):
        with self.lock:
            self._stop_locked()

    def status(self):
        with self.cond:
            return {
                "sid": self.sid,
                "mode": self.mode,
                "searching": self.searching,
                "best": self.best,
                "infos": [self.infos[k] for k in sorted(self.infos)],
            }

    def close(self):
        try:
            self._send("quit")
            self.p.wait(timeout=2)
        except Exception:
            self.p.kill()


ENGINE = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/options":
            self._json({"options": ENGINE.options, "saved": ENGINE.saved, "startFen": START_FEN})
        elif path == "/api/status":
            self._json(ENGINE.status())
        else:
            self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "ongeldige JSON"}, 400)
        path = self.path.split("?")[0]
        try:
            if path == "/api/state":
                self._json(ENGINE.state(body["fen"], body.get("move")))
            elif path == "/api/setoption":
                ENGINE.set_option(body["name"], body.get("value", ""))
                self._json({"ok": True})
            elif path == "/api/go":
                sid = ENGINE.go(body["fen"], body.get("mode", "analyse"),
                                int(body.get("depth") or 0), int(body.get("movetime") or 1000))
                self._json({"sid": sid})
            elif path == "/api/stop":
                ENGINE.stop()
                self._json({"ok": True})
            else:
                self.send_error(404)
        except Busy as e:
            self._json({"error": str(e)}, 409)
        except (EngineError, KeyError, ValueError) as e:
            self._json({"error": str(e) or "fout"}, 400)


def main():
    global ENGINE
    args = [a for a in sys.argv[1:]]
    port = 8000
    if "--port" in args:
        i = args.index("--port")
        port = int(args[i + 1])
        del args[i:i + 2]
    nobrowser = "--no-browser" in args
    args = [a for a in args if a != "--no-browser"]
    path = args[0] if args else DEFAULT_ENGINE
    if not os.path.isfile(path):
        sys.exit("Engine niet gevonden: %s" % path)
    ENGINE = Engine(path)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d" % port
    print("GUI draait op", url, "(Ctrl+C om te stoppen)")
    if not nobrowser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ENGINE.close()


if __name__ == "__main__":
    main()
