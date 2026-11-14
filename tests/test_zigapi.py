"""The Zig API facts must come from the compiler, not from the model.

A worker wrote `std.http.Server.init(stream_reader.interface(), &stream_writer.interface)` and a
reviewer rejected it as "Build-breaking" three times. It compiles: in Zig 0.15.2
`net.Stream.Reader.interface` is a method and `net.Stream.Writer.interface` is a field. Both the
worker and the reviewer were recalling a different Zig version.

These tests run the real compiler. They skip when Zig is absent rather than mocking it, because
a mocked compiler would assert the very thing the module exists to check.
"""
import os
import shutil

import pytest

from factory import zigapi

ZIG = os.environ.get("FACTORY_ZIG") or shutil.which("zig")

pytestmark = pytest.mark.skipif(ZIG is None, reason="Zig is not installed; facts cannot be measured")


def test_the_compiler_version_is_reported():
    assert zigapi.version(), "a Zig build must be able to state the compiler version"


def test_every_probe_compiles_on_the_installed_compiler():
    """The probes describe forms we tell the worker to use, so all of them must compile here.

    A failure means the prompt would be giving advice the compiler rejects -- worse than no
    advice at all.
    """
    result = zigapi.facts(force=True)
    assert result["failed"] == {}, f"probe(s) did not compile: {result['failed']}"
    assert len(result["ok"]) == len(zigapi.PROBES)


def test_the_reader_interface_is_a_method_and_the_writer_interface_is_a_field():
    """This is the exact pair the reviewer got wrong, so it is asserted by name."""
    result = zigapi.facts()
    assert "reader_interface_method" in result["ok"]
    assert "writer_interface_field" in result["ok"]


def test_the_allocating_writer_namespace_is_confirmed_by_compiling():
    """The reviewer called `std.io.Writer.Allocating` a namespace mismatch; it compiles."""
    result = zigapi.facts()
    assert "io_writer_alias" in result["ok"]


def test_the_prompt_block_states_the_version_and_the_accepted_forms():
    block = zigapi.prompt_block()
    assert "VERIFIED ZIG API FACTS" in block
    assert zigapi.version() in block
    assert "METHOD" in block and "FIELD" in block
    assert "do not use them" not in block, "nothing failed on this compiler"


def test_a_probe_that_cannot_compile_is_reported_not_hidden():
    """A form the compiler rejects must be named as unusable, never silently omitted."""
    accepted, message = zigapi._compile("const x: NoSuchType = 1;\n")
    assert accepted is False
    assert message, "a failed compile must produce the compiler's own message"
