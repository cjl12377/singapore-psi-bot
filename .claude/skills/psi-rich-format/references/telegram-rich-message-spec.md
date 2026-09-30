# Telegram Rich-Message API Spec (Bot API 9.5+)

Quoted / condensed from `core.telegram.org/bots/api` and the "Rich Text for Bots" announcement (`telegram.org/blog/watch-apps-and-more`, 2026-06-11). Not everything here has been tested on this bot; see SKILL.md for what has.

## Two message flavors

`sendMessage` accepts **either**:
- `text` + `parse_mode` — regular message, MarkdownV2 or HTML
- ~~`markdown` / `html` — rich message~~ **WRONG** (this is a common misconception). The rich tier is sent via a **separate `sendRichMessage` endpoint** (Bot API 9.5+). The body contains a `rich_message` parameter whose value is a **JSON-encoded string** wrapping an `InputRichMessage` object: `{"markdown": "..."}` or `{"html": "..."}` (plus optional `is_rtl`, `skip_entity_detection`).

**Don't confuse the two endpoints.** Sending a regular `sendMessage` with a `markdown` field will be ignored by the server. Sending `sendRichMessage` with a `text` field will be rejected (you cannot mix them). The two endpoints share the same chat/thread/notification/reply parameters, but the content goes in different fields.

## Rich Markdown style (the `markdown` field)

GitHub Flavored Markdown where possible. Can include arbitrary supported HTML tags inline.

```
**bold text**           __bold text__
*italic text*           _italic text_
~~strikethrough~~
||spoiler||
`inline code`
```code block```
> blockquote
>> nested blockquote
>>> deeper nested
**> expandable blockquote
   multi-line content
   ||
[link text](https://example.com)
```

### Markdown extras not in GFM (only in rich tier)

- `**> ... ||` — expandable blockquote (collapse/expand)
- `<u>...</u>` and `<ins>...</ins>` — underline (real, not escaped)
- `<sub>...</sub>` — subscript
- `<sup>...</sup>` — superscript
- Math: `\\(inline math\\)` and `\\[display math\\]`
- `| column | column |` + `|---|---|` — real tables
- `- [ ]` / `- [x]` — task lists
- `# H1`, `## H2`, `### H3` — headings with sizing
- `---` — horizontal rule
- Footnote syntax: `[^1]` and `[^1]: ...`
- `> [!NOTE]`, `> [!TIP]`, etc. — alert blockquotes

## Rich HTML style (the `html` field)

Supported tags (from the docs):

```
<a name="anchor"></a>           <b>, <strong>
<i>, <em>                       <u>, <ins>
<s>, <strike>, <del>            <span class="...">
<code>                          <pre>
<blockquote>                    <blockquote expandable>
<tg-spoiler>                    <tg-emoji emoji-id="...">
<a href="url">                  <a href="mailto:...">
<a href="tg://user?id=...">     <h1>, <h2>, <h3>, <h4>, <h5>, <h6>
<table><tr><th><td>             <ul>, <ol>, <li>
<input type="checkbox">         <hr>
<sub>, <sup>                    <math>...</math>
<aside>                         <details><summary>
```

## Limits (rich messages)

From the docs, rich messages are subject to:
- 32,768 characters total
- 1,024 characters per heading / line of a list
- 4,096 characters per URL
- Media must be in its own block; MIME type determines rendering
- Only HTTP/HTTPS URLs for media

## Entities (auto-detected unless `skip_entity_detection=True`)

Plain text gets these entities detected automatically:
- `text_link` (URLs)
- `text_mention` (`@username`)
- `hashtag` (`#foo`)
- `cashtag` (`$FOO`)
- `bot_command` (`/cmd`)
- `phone_number`
- `bank_card_number`
- `email`

Pass `skip_entity_detection=True` to disable; Telegram shows a "Open this link?" alert before opening inline links by default.
