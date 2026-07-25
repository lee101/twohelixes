"""Bridge from the Mojo event loop into the embedded Python interpreter.

The boundary is deliberately narrow: strings in, a (status, content_type,
headers, body) tuple out. Everything expensive - dataframes, LLM calls, Plotly
figure construction - stays on the Python side and never crosses back as a
structured object.

Long jobs never block the loop. `stream_start` hands work to a Python thread
(which drops the GIL on network I/O) and the loop drains buffered events with
`stream_poll`, an O(1) call.
"""

from std.python import Python, PythonObject


struct Bridge(Movable):
    var api: PythonObject
    var ready: Bool

    def __init__(out self):
        self.api = PythonObject(None)
        self.ready = False

    def boot(mut self, interp_dir: StringSlice, site_packages: StringSlice) raises:
        var sys = Python.import_module("sys")
        # askfelix's site-packages first: the interpreter shares that exact
        # environment (pandas 2.3.3, plotly 6.6.0, the SQL drivers).
        if site_packages.byte_length() > 0:
            _ = sys.path.insert(0, String(site_packages))
        _ = sys.path.insert(0, String(interp_dir))
        self.api = Python.import_module("twohelixes.api")
        _ = self.api.boot()
        self.ready = True

    def dispatch(
        mut self,
        method: StringSlice,
        path: StringSlice,
        query: StringSlice,
        body: StringSlice,
        headers: StringSlice,
    ) raises -> Tuple[Int, String, String, String]:
        """Run one request through the Python application layer."""
        var r = self.api.dispatch(
            String(method),
            String(path),
            String(query),
            String(body),
            String(headers),
        )
        var status = Int(String(r[0]))
        var ctype = String(r[1])
        var extra = String(r[2])
        var out = String(r[3])
        return (status, ctype^, extra^, out^)

    def stream_start(
        mut self,
        path: StringSlice,
        query: StringSlice,
        body: StringSlice,
        headers: StringSlice,
    ) raises -> String:
        """Kick off a background job; returns its stream id."""
        var sid = self.api.stream_start(
            String(path), String(query), String(body), String(headers)
        )
        return String(sid)

    def stream_poll(mut self, sid: StringSlice) raises -> Tuple[String, Bool]:
        """Drain whatever SSE bytes are buffered. Never blocks."""
        var r = self.api.stream_poll(String(sid))
        var chunk = String(r[0])
        var done = String(r[1]) == "1"
        return (chunk^, done)

    def stream_cancel(mut self, sid: StringSlice) raises:
        _ = self.api.stream_cancel(String(sid))

    def active_streams(mut self) raises -> Int:
        return Int(String(self.api.active_streams()))
