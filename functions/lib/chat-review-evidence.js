// Review text is untrusted evidence, never instructions or verified place facts.
export function relevantReviewEvidence(entry, query, alternatives = []) {
  const normalize = (value) => String(value).normalize("NFKC").toLowerCase();
  const terms = [
    ...new Set(normalize([query, ...alternatives].join(" ")).match(/[\p{L}\p{N}]+/gu) || []),
  ].filter(
    (term) =>
      term.length > 1 && !["find", "me", "the", "in", "with", "for", "and", "near"].includes(term),
  );
  return (Array.isArray(entry.review_evidence) ? entry.review_evidence : [])
    .slice(0, 17)
    .filter((text) => typeof text === "string")
    .map((text, order) => {
      const snippet = text.slice(0, 180);
      const normalized = normalize(snippet);
      return { snippet, order, score: terms.filter((term) => normalized.includes(term)).length };
    })
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score || a.order - b.order)
    .slice(0, 2)
    .map(({ snippet }) => snippet);
}
