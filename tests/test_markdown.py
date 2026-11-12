from tms_agent_kit.markdown import render_markdown


def link(target: str) -> str | None:
    if target.startswith("javascript"):
        return None
    return target if target.startswith("http") else "../" + target


def test_blocks() -> None:
    text = """## Summary

Works for **all** models: `a<b>`.
Same paragraph <script>x</script>.

Checks:
- one
  continued
  - nested
- two
1. first

| A | B |
|---|---|
| `x\\|y` | ok |

> quoted

```
# not a heading
```"""
    assert render_markdown(text, link) == (
        "<h4>Summary</h4>"
        "<p>Works for <b>all</b> models: <code>a&lt;b&gt;</code>. "
        "Same paragraph &lt;script&gt;x&lt;/script&gt;.</p>"
        "<p>Checks:</p>"
        "<ul><li>one continued<ul><li>nested</li></ul></li><li>two</li></ul>"
        "<ol><li>first</li></ol>"
        "<div class=scroll><table><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td><code>x|y</code></td><td>ok</td></tr></tbody></table></div>"
        "<blockquote><p>quoted</p></blockquote>"
        "<pre><code># not a heading</code></pre>"
    )


def test_links() -> None:
    html = render_markdown(
        "![shot](a/b.png) [log](logs/l.txt) [x](javascript:alert) ![remote](https://h/i.png) "
        "see https://h/p.",
        link,
    )
    assert html == (
        '<p><a href="../a/b.png" target="_blank"><img src="../a/b.png" alt="shot"></a> '
        '<a href="../logs/l.txt">log</a> x '
        '<a href="https://h/i.png" target="_blank" rel="noopener">remote</a> '
        'see <a href="https://h/p" target="_blank" rel="noopener">https://h/p</a>.</p>'
    )
