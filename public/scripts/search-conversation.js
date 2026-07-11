const normalizePhrase = (value) =>
  String(value || "")
    .trim()
    .toLowerCase()
    .replace(/-/g, " ")
    .replace(/\s+/g, " ");

const pluralize = (count, singular, plural = `${singular}s`) =>
  `${count} ${count === 1 ? singular : plural}`;

const joinTerms = (terms) => {
  if (terms.length === 0) return "";
  if (terms.length === 1) return terms[0];
  if (terms.length === 2) return `${terms[0]} and ${terms[1]}`;
  return `${terms.slice(0, -1).join(", ")}, and ${terms.at(-1)}`;
};

export function buildSearchConversation({ count = 0, parsed = {}, scopeLabel = "" } = {}) {
  const categories = Array.isArray(parsed.categories) ? parsed.categories : [];
  const vibes = Array.isArray(parsed.vibes) ? parsed.vibes : [];
  const terms = [...new Set([...vibes, ...categories].map(normalizePhrase).filter(Boolean))];
  const scope = scopeLabel ? ` in ${scopeLabel}` : "";

  if (count === 0) {
    const understood = terms.length > 0 ? ` for ${joinTerms(terms)}` : "";
    return `No places matched${understood}${scope}. Try removing a term or choosing a suggested refinement.`;
  }

  const descriptor = terms.length > 0 ? ` for ${joinTerms(terms)}` : "";
  return `I found ${pluralize(count, "place")}${descriptor}${scope}. Results are matched from saved place details and tags.`;
}

export function buildSearchSuggestions({ parsed = {}, results = [], limit = 4 } = {}) {
  const activeTerms = new Set(
    [
      ...(Array.isArray(parsed.categories) ? parsed.categories : []),
      ...(Array.isArray(parsed.vibes) ? parsed.vibes : []),
    ]
      .map(normalizePhrase)
      .filter(Boolean),
  );
  const candidates = new Map();

  const addCandidate = (value) => {
    const term = normalizePhrase(value);
    if (!term || activeTerms.has(term)) return;
    candidates.set(term, (candidates.get(term) || 0) + 1);
  };

  results.forEach(({ entry } = {}) => {
    if (!entry) return;
    addCandidate(entry.category);
    (Array.isArray(entry.vibe_tags) ? entry.vibe_tags : []).forEach(addCandidate);
  });

  if (candidates.size === 0 && activeTerms.size > 0) {
    return [{ action: "clear", label: "Clear search", query: "" }];
  }

  return [...candidates.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .slice(0, limit)
    .map(([query]) => ({
      action: "append",
      label: query.replace(/\b\w/g, (letter) => letter.toUpperCase()),
      query,
    }));
}
