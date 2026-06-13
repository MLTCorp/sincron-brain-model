"""MCP contract text guides generic hosts toward plug-and-play memory use."""

from pathlib import Path


def test_mcp_instructions_make_use_memories_the_content_path():
    source = Path("src/sincron_brain/server.py").read_text(encoding="utf-8")
    assert "Choose from synopses first" in source
    assert "call use_memories(ids)" in source
    assert "list_tags('soul')" in source
    assert "list_tags('preferences')" in source
    assert "Major Tags are primary retrieval routes" in source
    assert "read_memory(id) is neutral inspection/debug compatibility" in source


def test_tool_docstrings_distinguish_use_from_inspection():
    source = Path("src/sincron_brain/server.py").read_text(encoding="utf-8")
    assert "main plug-and-play path" in source
    assert "Compatibility/debug escape hatch" in source
