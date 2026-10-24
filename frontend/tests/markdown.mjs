import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({ server: { middlewareMode: true }, appType: "custom" });
const { ArticleMarkdown, articleOutline, safeArticleUrl } = await server.ssrLoadModule("/src/lib/article-markdown.tsx");
after(async () => { await server.close(); });

test("article rendering escapes HTML and rejects executable links", () => {
  const html = renderToStaticMarkup(React.createElement(ArticleMarkdown, { content: '<script>alert("x")</script>\n\n[Unsafe](javascript:alert)\n\n[Read more](https://example.com/story)' }));
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("&lt;script&gt;"));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('href="https://example.com/story"'));
  assert.ok(html.includes('rel="noopener noreferrer"'));
});

test("only public HTTP links without embedded credentials are allowed", () => {
  assert.equal(safeArticleUrl("data:text/html,bad"), null);
  assert.equal(safeArticleUrl("https://name:secret@example.com"), null);
  assert.equal(safeArticleUrl("https://example.com/read"), "https://example.com/read");
});

test("outline skips fenced code and preserves heading offsets", () => {
  const source = "Opening.\n\n## A useful section\n\nText.\n\n```\n## Code example\n```\n\n## The next step";
  const headings = articleOutline(source);
  assert.equal(headings.length, 2);
  assert.equal(headings[0].text, "A useful section");
  assert.equal(source.slice(headings[1].offset).split("\n")[0], "## The next step");
});
