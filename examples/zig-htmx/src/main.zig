//! Minimal Zig + HTMX application, and the reference a worker should follow.
//!
//! Two things here are not obvious and cost real time to discover:
//!
//!   1. `std.http.Server` handles ONE connection; it has no listener. Binding `std.net` yourself
//!      and calling `Server.init` per accepted connection is required. There is no
//!      `http.Server.listen` in Zig 0.15.2.
//!   2. `net.Stream.reader()` returns a struct whose `interface` is a METHOD, while
//!      `net.Stream.writer()` returns one whose `interface` is a FIELD. Mixing them is a
//!      compile error that reads like a type mismatch.
//!
//! HTMX needs one thing from the server: a handler that returns an HTML *fragment* when
//! `HX-Request: true` is present, and a full page otherwise. That single branch is the whole
//! integration; there is no client-side build step.
const std = @import("std");
const net = std.net;

const INDEX = @embedFile("index.html");

fn handle(conn: net.Server.Connection, allocator: std.mem.Allocator) !void {
    var read_buf: [8192]u8 = undefined;
    var write_buf: [8192]u8 = undefined;
    var reader = conn.stream.reader(&read_buf);
    var writer = conn.stream.writer(&write_buf);
    var server = std.http.Server.init(reader.interface(), &writer.interface);

    var request = server.receiveHead() catch return;
    const path = request.head.target;

    // The route decides the shape of the response here: /api/* returns a fragment that htmx
    // swaps in, everything else returns a full document. Reading HX-Request is the alternative
    // when one route must serve both, and it is how jetzig's middleware behaves.
    if (std.mem.startsWith(u8, path, "/api/hello")) {
        // A fragment, not a document: this is what hx-target swaps in.
        const body = "<p id=\"greeting\">Halo dari Zig, dikirim sebagai fragmen HTMX.</p>";
        try request.respond(body, .{
            .status = .ok,
            .extra_headers = &.{.{ .name = "content-type", .value = "text/html; charset=utf-8" }},
        });
    } else if (std.mem.eql(u8, path, "/api/health")) {
        try request.respond("{\"status\":\"ok\"}", .{
            .status = .ok,
            .extra_headers = &.{.{ .name = "content-type", .value = "application/json" }},
        });
    } else if (std.mem.eql(u8, path, "/") or std.mem.eql(u8, path, "/index.html")) {
        try request.respond(INDEX, .{
            .status = .ok,
            .extra_headers = &.{.{ .name = "content-type", .value = "text/html; charset=utf-8" }},
        });
    } else {
        try request.respond("Not found", .{ .status = .not_found });
    }
    _ = allocator;
    try writer.interface.flush();
}

pub fn main() !void {
    const allocator = std.heap.page_allocator;
    // PORT is set by the factory when it serves a verified build; 3000 is the local default.
    const port: u16 = blk: {
        const raw = std.process.getEnvVarOwned(allocator, "PORT") catch break :blk 3000;
        defer allocator.free(raw);
        break :blk std.fmt.parseInt(u16, raw, 10) catch 3000;
    };
    const addr = try net.Address.parseIp("127.0.0.1", port);
    var server = try addr.listen(.{ .reuse_address = true });
    defer server.deinit();

    while (true) {
        const conn = server.accept() catch continue;
        handle(conn, allocator) catch {};
        conn.stream.close();
    }
}
