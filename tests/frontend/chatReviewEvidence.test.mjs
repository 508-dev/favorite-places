import { describe, expect, it } from "vitest";
import { relevantReviewEvidence } from "../../functions/lib/chat-review-evidence.js";

describe("chat review evidence budget", () => {
  it("retains single-character CJK terms and folds Latin accents like retrieval", () => {
    expect(relevantReviewEvidence({ review_evidence: ["鴨料理很好吃", "茶很好喝"] }, "鴨")).toEqual(
      ["鴨料理很好吃"],
    );
    expect(relevantReviewEvidence({ review_evidence: ["鴨料理很好吃", "茶很好喝"] }, "茶")).toEqual(
      ["茶很好喝"],
    );
    expect(relevantReviewEvidence({ review_evidence: ["Lovely café upstairs"] }, "cafe")).toEqual([
      "Lovely café upstairs",
    ]);
  });

  it("keeps evidence supported by retrieval atmosphere aliases", () => {
    expect(
      relevantReviewEvidence(
        { review_evidence: ["Quiet upstairs seating", "Busy downstairs"] },
        "calm cafe",
      ),
    ).toEqual(["Quiet upstairs seating"]);
  });

  it("selects relevant snippets with Unicode support and a fixed payload budget", () => {
    const entry = {
      review_evidence: [
        "Noisy downstairs.",
        `Sesame pudding ${"x".repeat(400)}`,
        "Pudding upstairs",
        "Sesame pudding upstairs",
        "小籠湯包很好吃",
      ],
    };
    expect(relevantReviewEvidence(entry, "sesame pudding upstairs")).toEqual([
      "Sesame pudding upstairs",
      `Sesame pudding ${"x".repeat(165)}`,
    ]);
    expect(relevantReviewEvidence(entry, "小籠湯包")).toEqual(["小籠湯包很好吃"]);
    expect(relevantReviewEvidence(entry, "soup dumplings", ["小籠湯包"])).toEqual([
      "小籠湯包很好吃",
    ]);
    expect(relevantReviewEvidence(entry, "ramen")).toEqual([]);
    expect(relevantReviewEvidence({}, "ramen")).toEqual([]);
  });
});
