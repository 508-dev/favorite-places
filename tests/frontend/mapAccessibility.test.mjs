import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = () => readFileSync("src/lib/mapAccessibility.ts", "utf8");
const functionBody = (sourceText, functionName) =>
  sourceText.slice(
    sourceText.indexOf(`function ${functionName}`),
    sourceText.indexOf(`function ${functionName}`) + 800,
  );

describe("map accessibility patching", () => {
  it("avoids layout-triggering text reads and preserves existing labelled anchors", () => {
    const mapAccessibility = source();

    expect(mapAccessibility).not.toContain("innerText");
    expect(mapAccessibility).toContain("anchor.textContent?.trim()");
    expect(mapAccessibility).toContain('"aria-label", "aria-labelledby"');
    expect(functionBody(mapAccessibility, "patchAnchors")).toContain(
      'anchor.getAttribute("aria-labelledby")?.trim()',
    );
    expect(functionBody(mapAccessibility, "anchorAccessibleName")).not.toContain(
      'anchor.getAttribute("aria-labelledby")?.trim()',
    );
  });

  it("coalesces mutation patches through animation frames", () => {
    const mapAccessibility = source();

    expect(mapAccessibility).toContain("let pendingPatchFrame: number | null = null;");
    expect(mapAccessibility).toContain("const schedulePatch = () => {");
    expect(mapAccessibility).toContain("window.requestAnimationFrame");
    expect(mapAccessibility).toContain("new MutationObserver(schedulePatch)");
    expect(mapAccessibility).toContain("window.setTimeout(schedulePatch, delay)");
    expect(mapAccessibility).toContain("window.cancelAnimationFrame(pendingPatchFrame)");
  });
});
