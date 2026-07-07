import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MarkdownContent } from "./MarkdownContent";

function render(markdown: string): string {
  return renderToStaticMarkup(createElement(MarkdownContent, null, markdown));
}

test("markdown table merge markers render as spans without leaking markers", () => {
  const html = render(`| A | B | C |
| --- | --- | --- |
| section | ::merge-left:: | tail |
| top | value | keep |
| ::merge-up:: | next | keep |
| ::merge-up-left:: | ::merge-up-left:: | keep |
| empty |  | keep |`);

  assert.match(html, /colSpan="2"/);
  assert.match(html, /rowSpan="3"/);
  assert.doesNotMatch(html, /::merge-left::|::merge-up::/);
  assert.match(html, /<td colSpan="1" rowSpan="1"><\/td>/);
});

test("markdown content escapes raw html", () => {
  const html = render(`<script>alert("x")</script>

| A | B |
| --- | --- |
| <b>raw</b> | text |`);

  assert.doesNotMatch(html, /<script>|<b>raw<\/b>/);
  assert.match(html, /&lt;script&gt;alert/);
});
