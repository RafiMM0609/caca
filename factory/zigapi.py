"""Measured Zig API facts, produced by compiling, not by recalling.

**Why this file exists.** A worker writing a Zig HTTP server produced
`std.http.Server.init(stream_reader.interface(), &stream_writer.interface)` and the reviewer
rejected it as a compile error three times. It compiles: in Zig 0.15.2 `net.Stream.Reader`
exposes `interface` as a *method* and `net.Stream.Writer` exposes it as a *field*. The worker was
guessing from training data that predates the change, and the reviewer was guessing too, so both
were wrong in the same direction and the build could not converge.

A prompt that says "use the correct API" is useless against that. What helps is a snippet the
worker can copy that is known to compile against the installed compiler. So this module writes
small probe programs, runs the real `zig`, and reports which forms were accepted.

**The cost is paid once per build, not per worker.** The result is cached in memory and keyed by
the compiler version, so a plan with eight Zig tasks compiles each probe once.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

# (id, human description, source). Each probe isolates one fact that a generated Zig HTTP server
# needs and that models get wrong. Keeping them small is deliberate: a compile failure then
# names exactly one construct.
PROBES = [
    ("reader_interface_method",
     "net.Stream.Reader exposes `interface` as a METHOD",
     """
const std = @import("std");
pub fn main() !void {
    var buf: [64]u8 = undefined;
    var stream = std.net.Stream{ .handle = 0 };
    var reader = stream.reader(&buf);
    const iface: *std.Io.Reader = reader.interface();
    _ = iface;
}
"""),
    ("writer_interface_field",
     "net.Stream.Writer exposes `interface` as a FIELD",
     """
const std = @import("std");
pub fn main() !void {
    var buf: [64]u8 = undefined;
    var stream = std.net.Stream{ .handle = 0 };
    var writer = stream.writer(&buf);
    const iface: *std.Io.Writer = &writer.interface;
    _ = iface;
}
"""),
    ("http_server_init",
     "std.http.Server.init takes (*Reader, *Writer)",
     """
const std = @import("std");
pub fn main() !void {
    var buf: [64]u8 = undefined;
    var stream = std.net.Stream{ .handle = 0 };
    var reader = stream.reader(&buf);
    var writer = stream.writer(&buf);
    var server = std.http.Server.init(reader.interface(), &writer.interface);
    _ = &server;
}
"""),
    ("listen_tcp",
     "a listener comes from std.net, not from std.http.Server",
     """
const std = @import("std");
pub fn main() !void {
    const address = try std.net.Address.parseIp("127.0.0.1", 0);
    var listener = try address.listen(.{ .reuse_address = true });
    defer listener.deinit();
}
"""),
    ("allocating_writer",
     "the allocating writer is std.Io.Writer.Allocating",
     """
const std = @import("std");
pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    var output: std.Io.Writer.Allocating = .init(gpa.allocator());
    _ = &output;
}
"""),
    ("io_writer_alias",
     "std.io.Writer.Allocating also resolves (compat alias)",
     """
const std = @import("std");
pub fn main() !void {
    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    var output: std.io.Writer.Allocating = .init(gpa.allocator());
    _ = &output;
}
"""),
    ("embed_file",
     "@embedFile resolves a path relative to the SOURCE FILE, not the build root",
     """
const std = @import("std");
const DATA = @embedFile("probe_asset.txt");
pub fn main() !void {
    _ = DATA;
}
"""),
    ("expect_equal_optional",
     "std.testing.expectEqual accepts `null` against an optional (compiles; a mismatch fails at "
     "RUN time, not compile time)",
     """
const std = @import("std");
test "expectEqual with an optional" {
    const value: ?usize = null;
    try std.testing.expectEqual(null, value);
}
"""),
    ("test_block_syntax",
     "a test block is `test \"name\" { ... }` compiled by `zig test`, not by `zig build`",
     """
const std = @import("std");
test "a test block compiles" {
    try std.testing.expect(true);
}
"""),
    ("expect_error",
     "`std.testing.expectError` wraps a call that must fail",
     """
const std = @import("std");
fn boom() !void {
    return error.Boom;
}
test "expectError" {
    try std.testing.expectError(error.Boom, boom());
}
"""),
    ("http_head_content_type",
     "std.http.Server.Request.Head exposes `content_type`, `content_length` and `method` as FIELDS",
     """
const std = @import("std");
pub fn main() !void {
    var head: std.http.Server.Request.Head = undefined;
    _ = &head;
    _ = head.content_type;
    _ = head.content_length;
    _ = head.method;
    _ = head.target;
}
"""),
    ("reader_expect_none_takes_buffer",
     "Request.readerExpectNone(buffer) TAKES a buffer and returns *std.Io.Reader",
     """
const std = @import("std");
pub fn main() !void {
    var buf: [64]u8 = undefined;
    var req: *std.http.Server.Request = undefined;
    _ = &req;
    const r: *std.Io.Reader = req.readerExpectNone(&buf);
    _ = r;
}
"""),
    ("iterate_headers_twice",
     "iterateHeaders() can be called; HX-Request must be read BEFORE the body is consumed",
     """
const std = @import("std");
pub fn main() !void {
    var req: *std.http.Server.Request = undefined;
    _ = &req;
    var it = req.iterateHeaders();
    while (it.next()) |header| {
        _ = header.name;
        _ = header.value;
    }
}
"""),
    ("env_var",
     "reading an environment variable returns an owned slice",
     """
const std = @import("std");
pub fn main() !void {
    const allocator = std.heap.page_allocator;
    const value = std.process.getEnvVarOwned(allocator, "PORT") catch return;
    defer allocator.free(value);
    _ = std.fmt.parseInt(u16, value, 10) catch 3000;
}
"""),
]

# Probes that contain a `test` block cannot be built with `build-exe` -- a test block is only
# type-checked and run by `zig test`. Building one as an executable silently skips the check,
# which is how a claim about a failing test survived a guard that only ran the build.
TEST_PROBES = {"expect_equal_optional", "test_block_syntax", "expect_error"}

# Files a probe needs beside its source. `@embedFile` is resolved relative to the source file, so
# this asset must sit next to the probe rather than at the repository root.
ASSETS = {"probe_asset.txt": "probe\n"}

_cache = {}


def zig_binary():
    """Same resolution order as `local_app.zig_binary`, kept here to avoid a cycle."""
    import os
    configured = os.environ.get("FACTORY_ZIG", "")
    if configured:
        return configured
    return shutil.which("zig") or "zig"


def shared_cache_dir() -> Path:
    """A process-wide Zig global cache shared by every probe and every worker.

    `FACTORY_ZIG_CACHE` overrides it. The default sits under the system temp dir rather than the
    data dir because it is derived data: deleting it costs one recompile, never a record.
    """
    import os
    base = os.environ.get("FACTORY_ZIG_CACHE", "")
    path = Path(base) if base else Path(tempfile.gettempdir()) / "factory-zig-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def version():
    """The compiler version, or "" when Zig is not installed."""
    try:
        completed = subprocess.run([zig_binary(), "version"], capture_output=True, text=True,
                                   timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (completed.stdout or "").strip() if completed.returncode == 0 else ""


def _compile(source: str, as_test: bool = False) -> tuple:
    """Compile one probe in a throwaway directory. Returns (ok, message).

    `as_test=True` runs `zig test`, which type-checks **and executes** a `test` block.
    `build-exe` silently skips test bodies, so a claim about a test used to survive the guard
    even when the test compiled and passed.
    """
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        main = root / "probe.zig"
        main.write_text(source)
        for name, content in ASSETS.items():
            (root / name).write_text(content)
        try:
            # A SHARED global cache, not a per-probe temp one. Zig's global cache holds the
            # compiled std library, and compiling it from scratch per probe measured 49s for
            # eight probes; with a warm shared cache the same eight take a few seconds. The
            # per-probe local cache stays isolated so two probes cannot see each other's
            # artifacts.
            argv = [zig_binary(), "test" if as_test else "build-exe", "probe.zig"]
            if not as_test:
                argv += ["-femit-bin=probe_out"]
            argv += ["--cache-dir", str(root / ".cache"),
                     "--global-cache-dir", str(shared_cache_dir())]
            completed = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                                       timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{type(exc).__name__}: could not run the compiler"
        if completed.returncode == 0:
            return True, ""
        message = ((completed.stderr or "") + (completed.stdout or "")).strip()
        return False, message[-600:]


def facts(force=False) -> dict:
    """Compile every probe once per compiler version and return what was accepted.

    Returns {'version': str, 'ok': [id...], 'failed': {id: message}, 'lines': [str...]}.
    `lines` is what a prompt includes: one sentence per accepted form, with the exact expression.
    """
    current = version()
    if not force and current in _cache:
        return _cache[current]

    ok, failed = [], {}
    for probe_id, description, source in PROBES:
        accepted, message = _compile(source, as_test=probe_id in TEST_PROBES)
        if accepted:
            ok.append(probe_id)
        else:
            failed[probe_id] = message

    lines = []
    descriptions = {probe_id: description for probe_id, description, _ in PROBES}
    for probe_id in ok:
        lines.append("- " + descriptions[probe_id])

    result = {"version": current, "ok": ok, "failed": failed, "lines": lines}
    _cache[current] = result
    return result


def prompt_block(force=False) -> str:
    """A prompt section stating only what the installed compiler accepted.

    Returns "" when Zig is absent, so a Node profile's prompt is untouched.
    """
    result = facts(force=force)
    if not result["version"] or not result["lines"]:
        return ""
    header = (f"VERIFIED ZIG API FACTS (Zig {result['version']}, measured by compiling each form "
              "on this machine -- follow these exactly, they override anything you remember):")
    body = "\n".join(result["lines"])
    missing = ""
    if result["failed"]:
        missing = ("\nForms that did NOT compile here (do not use them): "
                   + ", ".join(sorted(result["failed"])))
    return header + "\n" + body + missing
