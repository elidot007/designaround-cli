// Minimal, safe Markdown renderer for assistant replies: escapes all HTML first,
// then supports headings, bold, italics, inline code, code blocks, lists, tables
// and links (http/https only). No raw HTML passes through.
(function () {
  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }
  function render(md) {
    const lines = esc(md || "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^```/.test(line)) {
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push("<pre><code>" + buf.join("\n") + "</code></pre>");
        continue;
      }
      if (/^\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const row = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
        const head = row(line);
        i += 2;
        const body = [];
        while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) body.push(row(lines[i++]));
        out.push("<table><thead><tr>" + head.map((c) => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>" + body.map((r) => "<tr>" + r.map((c) => "<td>" + c + "</td>").join("") + "</tr>").join("") + "</tbody></table>");
        continue;
      }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        const n = Math.min(4, h[1].length + 1);
        out.push("<h" + n + ">" + inline(h[2]) + "</h" + n + ">");
        i++;
        continue;
      }
      if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\./.test(line);
        const items = [];
        while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) items.push(inline(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, "")));
        out.push((ordered ? "<ol>" : "<ul>") + items.map((t) => "<li>" + t + "</li>").join("") + (ordered ? "</ol>" : "</ul>"));
        continue;
      }
      if (!line.trim()) {
        i++;
        continue;
      }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\s*([-*]|\d+\.)\s+|\|)/.test(lines[i])) para.push(inline(lines[i++]));
      if (para.length) out.push("<p>" + para.join("<br>") + "</p>");
      else i++;
    }
    return out.join("\n");
  }
  window.renderMarkdown = render;
})();
