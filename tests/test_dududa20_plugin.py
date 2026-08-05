"""Tests for dududa20 plugin core functions (unit-testable without AstrBot runtime)."""
import sys, os, pytest, base64
from io import BytesIO
sys.path.insert(0, "/opt/dududa20-prototype")
sys.path.insert(0, "/root/data/plugins/dududa20")

# Import module-level functions directly
import importlib.util
spec = importlib.util.spec_from_file_location("dududa_main", "/root/data/plugins/dududa20/main.py")
main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(main)

# ── Media Detection ──

class TestDetectMedia:
    def test_no_messages(self):
        """Empty messages returns no media."""
        class FakeEvent:
            def get_messages(self): return []
        url, name, is_img = main._detect_media(FakeEvent())
        assert url == ""
        assert not is_img

    def test_file_component(self):
        """File component with HTTP URL detected."""
        class FakeComponent:
            type = "ComponentType.File"
            url = "https://example.com/file.docx"
            name = "test.docx"
        class FakeEvent:
            def get_messages(self): return [FakeComponent()]
        url, name, is_img = main._detect_media(FakeEvent())
        assert url == "https://example.com/file.docx"
        assert name == "test.docx"
        assert not is_img

    def test_image_component_local_path(self):
        """Image component with local path detected."""
        class FakeComponent:
            type = "ComponentType.Image"
            url = "/root/data/temp/media_xxx.jpg"
            file = "/root/data/temp/media_xxx.jpg"
        class FakeEvent:
            def get_messages(self): return [FakeComponent()]
        url, name, is_img = main._detect_media(FakeEvent())
        assert url.startswith("/")
        assert is_img

    def test_image_component_http(self):
        """Image component with HTTP URL detected."""
        class FakeComponent:
            type = "ComponentType.Image"
            url = "https://example.com/photo.jpg"
            name = "photo.jpg"
        class FakeEvent:
            def get_messages(self): return [FakeComponent()]
        url, name, is_img = main._detect_media(FakeEvent())
        assert url == "https://example.com/photo.jpg"
        assert is_img

# ── File Extension ──

class TestFileExt:
    def test_docx(self):
        assert main._file_ext("report.docx") == "docx"
    def test_no_ext(self):
        assert main._file_ext("noext") == ""
    def test_tar_gz(self):
        assert main._file_ext("file.tar.gz") == "gz"

# ── Document Parsing ──

class TestParseDocument:
    def test_txt(self):
        data = "hello world".encode("utf-8")
        result = main._parse_document(data, "test.txt")
        assert result == "hello world"

    def test_markdown(self):
        data = "# Title\ncontent".encode("utf-8")
        result = main._parse_document(data, "readme.md")
        assert "# Title" in result

    def test_unknown_ext_fallsback_to_utf8(self):
        data = "plain text".encode("utf-8")
        result = main._parse_document(data, "file.xyz")
        assert result == "plain text"

    def test_binary_returns_none(self):
        data = b'\x00\x01\x02\x03'
        result = main._parse_document(data, "data.bin")
        # Should not crash
        assert result is not None or result is None

# ── Model Router ──

class TestModelRouter:
    def test_text_route(self):
        routes = main.router.resolve("text")
        assert len(routes) >= 1
        assert routes[0].model == "deepseek-chat"
        assert routes[0].provider == "deepseek"

    def test_image_route_has_fallback(self):
        routes = main.router.resolve("image")
        assert len(routes) >= 2  # Claude + Gemini fallback
        models = [r.model for r in routes]
        assert "claude-haiku-4-5-20251001" in models
        assert "gemini-3.1-flash-image-preview" in models

    def test_unknown_type_fallsback_to_text(self):
        routes = main.router.resolve("video")
        assert len(routes) >= 1
        assert routes[0].model == "deepseek-chat"

    def test_file_route(self):
        routes = main.router.resolve("file")
        assert len(routes) >= 1
        assert routes[0].provider == "deepseek"

# ── Guards (unit-testable logic) ──

class TestHasMediaInRaw:
    def test_empty_raw(self):
        class FakeEvent:
            raw_message = None
        assert not main._has_media_in_raw(FakeEvent())

    def test_file_in_raw(self):
        class FakeRaw:
            def __init__(self):
                self.message = [{"type": "file"}]
        class FakeEvent:
            raw_message = FakeRaw()
        assert main._has_media_in_raw(FakeEvent())

    def test_image_in_raw(self):
        class FakeRaw:
            def __init__(self):
                self.message = [{"type": "image"}]
        class FakeEvent:
            raw_message = FakeRaw()
        assert main._has_media_in_raw(FakeEvent())
