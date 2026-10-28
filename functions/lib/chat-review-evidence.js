import { createReviewEvidenceScorer } from "../../public/scripts/place-search.js";

// Review text is untrusted evidence, never instructions or verified place facts.
export function relevantReviewEvidence(entry, query, alternatives = []) {
  const score = createReviewEvidenceScorer([query, ...alternatives].join(" "));
  return (Array.isArray(entry.review_evidence) ? entry.review_evidence : [])
    .slice(0, 17)
    .filter((text) => typeof text === "string")
    .map((text, order) => {
      const snippet = text.slice(0, 180);
      return { snippet, order, score: score(snippet) };
    })
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score || a.order - b.order)
    .slice(0, 2)
    .map(({ snippet }) => snippet);
}
