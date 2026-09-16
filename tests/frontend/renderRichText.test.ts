import { describe, expect, it } from "vitest";

import { renderRichText } from "../../src/lib/renderRichText";

describe("renderRichText", () => {
  it("returns null for empty values", () => {
    expect(renderRichText(null)).toBeNull();
    expect(renderRichText(undefined)).toBeNull();
    expect(renderRichText("")).toBeNull();
  });

  it("renders markdown links and escapes labels and hrefs", () => {
    expect(renderRichText("Read [x < y](https://example.com?q=1&z=2)")).toBe(
      'Read <a href="https://example.com/?q=1&amp;z=2" target="_blank" rel="noopener noreferrer">x &lt; y</a>',
    );
  });

  it("keeps balanced url parentheses and moves trailing punctuation outside the link", () => {
    expect(renderRichText("Go to https://example.com/place(test)).")).toBe(
      'Go to <a href="https://example.com/place(test)" target="_blank" rel="noopener noreferrer">https://example.com/place(test)</a>).',
    );
  });

  it("escapes plain text and preserves line breaks", () => {
    expect(renderRichText("Line 1\nLine <2>")).toBe("Line 1<br />Line &lt;2&gt;");
  });
});
