"""Operator-owned acceptance check; never execute generated scripts on the controller."""
import os
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for key, value in attrs:
            if key in ("src", "href") and value:
                self.links.append(value)


root = Path.cwd()
assert (root / "index.html").is_file(), "index.html is required"
pages = list(root.rglob("*.html"))
for file in pages:
    parser = Page()
    parser.feed(file.read_text())
    assert "title" in parser.tags, f"Page needs a title: {file.name}"
    for link in parser.links:
        url = urlsplit(link)
        if url.scheme or url.netloc or not url.path:
            continue
        path = (root / unquote(url.path).lstrip("/") if url.path.startswith("/") else file.parent / unquote(url.path)).resolve()
        assert path.is_relative_to(root), "Link escapes website root"
        assert path.exists(), f"Missing local asset: {link}"
out = Path(os.environ["FACTORY_OUTPUT"])
for file in root.rglob("*"):
    assert not file.is_symlink(), "Symlinks are forbidden"
    if file.is_file():
        destination = out / file.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, destination)
print(f"PASS: {len(pages)} HTML pages; required entrypoint, titles and local asset references")

