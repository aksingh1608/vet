# Red-team fixture sites

Five vendor homepages that each try to steer an AI evaluator through a different channel.
Every payload contains a unique marker word (`MARKER-n`) so tests can assert it never reaches
the LLM input. Used by `tests/test_guard.py`.

| File | Channel | Technique |
|---|---|---|
| `01_visible.html` | visible text | A plain paragraph telling AI evaluators what to conclude |
| `02_css_hidden.html` | CSS-hidden text | Off-screen div, zero font size, class hidden by a `<style>` rule |
| `03_comment_meta.html` | HTML comment, meta tag | Instructions in a comment and a custom meta tag |
| `04_attributes_colour.html` | alt/title attributes, same-colour text | Image alt text, link title, white-on-white span |
| `05_tag_escape.html` | visible text | Tries to close `</untrusted_content>` and open a fake system block; also contains ordinary AI product copy and an llms.txt link, which must NOT be flagged |
