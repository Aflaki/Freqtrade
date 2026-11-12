

def test_confirm_texts_are_safe_in_html_attributes():
    """A confirmation text put into a double-quoted onclick/onsubmit attribute breaks it (JSON quotes, or an
    apostrophe in English) and the action then runs WITHOUT asking. They must be single-quoted with |tojson."""
    import glob
    import re
    bad = []
    for p in glob.glob("app/templates/**/*.html", recursive=True):
        for m in re.finditer(r'on(click|submit)="[^"]*confirm\([^"]*\{\{', open(p).read()):
            bad.append(f"{p}: {m.group(0)[:80]}")
    assert not bad, bad
