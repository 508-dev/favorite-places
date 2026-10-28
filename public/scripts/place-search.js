const STOP_WORDS = new Set([
  "a",
  "an",
  "and",
  "around",
  "at",
  "best",
  "for",
  "in",
  "near",
  "of",
  "on",
  "the",
  "to",
  "with",
]);

const CATEGORY_ALIASES = new Map([
  ["bar", ["bar", "pub", "cocktail"]],
  ["cafe", ["cafe", "cafes", "coffee", "coffee shop", "coffee shops", "espresso"]],
  ["museum", ["museum", "museums", "gallery", "galleries"]],
  ["restaurant", ["restaurant", "restaurants", "dinner", "lunch", "food", "eat", "eats"]],
  ["shop", ["shop", "shops", "store", "stores", "shopping"]],
]);

const CATEGORY_MATCH_TERMS = new Map([
  ["bar", ["bar", "pub", "cocktail"]],
  ["cafe", ["cafe", "coffee", "coffee-shop", "coffee shop", "bakery", "tea"]],
  ["museum", ["museum", "gallery", "art-gallery", "art gallery"]],
  ["restaurant", ["restaurant", "food", "dining", "eatery"]],
  ["shop", ["shop", "store", "market"]],
]);

const VIBE_ALIASES = new Map([
  ["cheap-eats", ["cheap", "affordable", "budget", "inexpensive", "cheap eats"]],
  ["cozy", ["cozy", "cosy", "good vibes", "warm", "homey"]],
  ["date-night", ["date night", "date", "romantic", "special occasion"]],
  ["design-forward", ["design", "stylish", "beautiful", "aesthetic", "good vibes"]],
  ["family-friendly", ["family", "kids", "children"]],
  ["group-friendly", ["group", "friends", "share", "shared"]],
  ["hidden-gem", ["hidden", "hidden gem", "underrated"]],
  ["laptop-friendly", ["laptop", "work", "wifi", "wi fi", "outlets"]],
  ["late-night", ["late night", "late", "nightlife"]],
  ["lively", ["lively", "buzzy", "fun", "good vibes"]],
  ["local-favorite", ["local", "local favorite", "regulars", "good vibes"]],
  ["outdoor-seating", ["outdoor", "patio", "terrace", "outside"]],
  ["quiet", ["quiet", "calm", "peaceful", "chill"]],
  ["rainy-day", ["rainy", "rainy day", "indoor", "indoors"]],
  ["scenic", ["scenic", "view", "views", "waterfront", "rooftop"]],
  ["slow-afternoon", ["slow", "afternoon", "linger"]],
  ["solo-friendly", ["solo", "alone", "counter"]],
  ["splurge", ["splurge", "expensive", "fine dining", "michelin"]],
  ["touristy-but-worth-it", ["touristy", "worth it", "iconic", "landmark"]],
]);

const VIBE_ALIAS_GROUPS = new Map();
for (const [vibe, aliases] of VIBE_ALIASES) {
  for (const alias of aliases) {
    const normalizedAlias = normalizeText(alias);
    if (!normalizedAlias) {
      continue;
    }

    if (!VIBE_ALIAS_GROUPS.has(normalizedAlias)) {
      VIBE_ALIAS_GROUPS.set(normalizedAlias, new Set());
    }

    VIBE_ALIAS_GROUPS.get(normalizedAlias).add(vibe);
  }
}

const SORTED_VIBE_ALIASES = [...VIBE_ALIAS_GROUPS.keys()].sort((left, right) => {
  const tokenDelta = tokenize(right).length - tokenize(left).length;
  if (tokenDelta !== 0) {
    return tokenDelta;
  }

  return right.length - left.length || left.localeCompare(right);
});

const LOCATION_ALIASES = new Map([
  ["new-york-new-york-usa", ["nyc", "new york city"]],
  ["san-francisco-california-usa", ["sf", "san fran", "san francisco"]],
  ["los-angeles-california-usa", ["la", "los angeles"]],
  ["tokyo-japan", ["tokyo"]],
]);

const cachedIndexPromises = new Map();

export async function loadSearchIndex(url = "/data/search-index.json") {
  if (!cachedIndexPromises.has(url)) {
    cachedIndexPromises.set(
      url,
      fetch(url)
        .then((response) => {
          if (!response.ok) {
            throw new Error(`Search index request failed: ${response.status}`);
          }
          return response.json();
        })
        .then(prepareSearchIndex)
        .catch((error) => {
          cachedIndexPromises.delete(url);
          throw error;
        }),
    );
  }

  return cachedIndexPromises.get(url);
}

export function prepareSearchIndex(index) {
  const guides = Array.isArray(index?.guides) ? index.guides : [];
  const entries = Array.isArray(index?.entries) ? index.entries : Array.isArray(index) ? index : [];
  const normalizedGuides = guides.map((guide) => ({
    ...guide,
    _searchText: normalizeText(guide.search_text || guide.title || ""),
    _tokens: tokenize(guide.search_text || guide.title || ""),
    _aliases: guideAliases(guide),
  }));
  const guideBySlug = new Map(normalizedGuides.map((guide) => [guide.slug, guide]));
  const normalizedEntries = entries.map((entry) => ({
    ...entry,
    tags: Array.isArray(entry.tags) ? entry.tags : [],
    vibe_tags: Array.isArray(entry.vibe_tags) ? entry.vibe_tags : [],
    _searchText: normalizeText(
      [entry.search_text || "", ...(entry.review_evidence || [])].join(" "),
    ),
    _reviewText: normalizeText((entry.review_evidence || []).join(" ")),
    _name: normalizeText(entry.name || ""),
    _category: normalizeText(entry.category || ""),
    _city: normalizeText(entry.city || ""),
    _country: normalizeText(entry.country || ""),
    _guideTitle: normalizeText(entry.guide_title || ""),
    _neighborhood: normalizeText(entry.neighborhood || ""),
    _note: normalizeText([entry.note || "", entry.why_recommended || ""].join(" ")),
    _nameTokens: tokenize(entry.name || ""),
    _categoryTokens: tokenize(entry.category || ""),
    _cityTokens: tokenize(entry.city || ""),
    _countryTokens: tokenize(entry.country || ""),
    _guideTitleTokens: tokenize(entry.guide_title || ""),
    _neighborhoodTokens: tokenize(entry.neighborhood || ""),
    _noteTokens: tokenize([entry.note || "", entry.why_recommended || ""].join(" ")),
    _searchTokens: tokenize([entry.search_text || "", ...(entry.review_evidence || [])].join(" ")),
    _tagTokens: tokenizeList([
      ...(Array.isArray(entry.tags) ? entry.tags : []),
      ...(Array.isArray(entry.vibe_tags) ? entry.vibe_tags : []),
    ]),
    _guide: guideBySlug.get(entry.guide_slug),
  }));

  return {
    ...index,
    guides: normalizedGuides,
    entries: normalizedEntries,
    guideBySlug,
  };
}

export function searchPlaces(query, options = {}) {
  const index = options.index
    ? prepareSearchIndexOnce(options.index)
    : prepareSearchIndex({ entries: [] });
  const parsed = parseQuery(query, index, options);
  const activeFilters = options.activeFilters || {};
  const scopedEntries = index.entries.filter((entry) => {
    if (options.scope === "guide" && options.guideSlug && entry.guide_slug !== options.guideSlug) {
      return false;
    }
    if (
      options.scope !== "guide" &&
      parsed.guideSlugs.size > 0 &&
      !parsed.guideSlugs.has(entry.guide_slug)
    ) {
      return false;
    }
    if (
      activeFilters.tag &&
      !normalizedList([...entry.tags, ...entry.vibe_tags]).includes(
        normalizeToken(activeFilters.tag),
      )
    ) {
      return false;
    }
    if (
      activeFilters.neighborhood &&
      normalizeToken(entry.neighborhood || "") !== normalizeToken(activeFilters.neighborhood)
    ) {
      return false;
    }
    if (activeFilters.topPicksOnly && !entry.top_pick) {
      return false;
    }
    return true;
  });

  const results = scopedEntries
    .map((entry) => scoreEntry(entry, parsed))
    .filter((result) => parsed.tokens.length === 0 || result.score > 0)
    .sort((left, right) => right.score - left.score || curatedSort(left.entry, right.entry));

  return {
    query,
    count: results.length,
    parsed: {
      categories: [...parsed.categories],
      guideSlugs: [...parsed.guideSlugs],
      unmatchedTerms: parsed.unmatchedTerms,
      vibes: [...parsed.vibes],
    },
    results,
  };
}

export function searchGuides(query, options = {}) {
  const index = options.index
    ? prepareSearchIndexOnce(options.index)
    : prepareSearchIndex({ guides: [] });
  const normalizedQuery = normalizeText(query);
  const tokens = tokenize(normalizedQuery);
  if (tokens.length === 0) {
    return [];
  }

  return index.guides
    .map((guide) => {
      let score = 0;
      const matchedSignals = [];
      for (const alias of guide._aliases) {
        if (containsPhrase(normalizedQuery, alias)) {
          score += alias === normalizeText(guide.title) ? 40 : 28;
          matchedSignals.push("guide");
        }
      }
      for (const token of tokens) {
        const matchStrength = scoreTokenMatch(token, guide._tokens);
        if (matchStrength > 0) {
          score += matchStrength === 1 ? 6 : 4;
          matchedSignals.push("guide");
        }
      }
      return { guide, matchedSignals: [...new Set(matchedSignals)], score };
    })
    .filter((result) => result.score > 0)
    .sort(
      (left, right) =>
        right.score - left.score || left.guide.title.localeCompare(right.guide.title),
    );
}

export function normalizeText(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/&/g, " and ")
    .replace(/['']/g, "")
    .replace(/[^\p{L}\p{N}\p{M}]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

// Keep prompt evidence selection aligned with lexical and atmosphere retrieval.
export function createReviewEvidenceScorer(query) {
  const normalizedQuery = normalizeText(query);
  const terms = [...new Set(tokenize(normalizedQuery))].filter(
    (term) =>
      (term.length > 1 ||
        /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(term)) &&
      !STOP_WORDS.has(term) &&
      !["find", "me"].includes(term),
  );
  const aliases = [...VIBE_ALIASES.values()]
    .filter((group) => group.some((alias) => containsPhrase(normalizedQuery, normalizeText(alias))))
    .flat()
    .map(normalizeText);
  return (text) => {
    const normalized = normalizeText(text);
    const tokens = tokenize(normalized);
    return (
      terms.filter((term) => scoreTokenMatch(term, tokens) > 0).length +
      Number(aliases.some((alias) => containsPhrase(normalized, alias)))
    );
  };
}

function parseQuery(query, index, options) {
  const normalizedQuery = normalizeText(query);
  const tokens = tokenize(normalizedQuery);
  const consumedTokens = new Set();
  const categories = new Set();
  const vibes = new Set();
  const vibeGroups = [];
  const guideSlugs = new Set();

  for (const [category, aliases] of CATEGORY_ALIASES) {
    if (aliases.some((alias) => consumePhrase(normalizedQuery, alias, consumedTokens))) {
      categories.add(category);
    }
  }

  for (const alias of SORTED_VIBE_ALIASES) {
    if (!consumeDisjointPhrase(normalizedQuery, alias, consumedTokens)) {
      continue;
    }

    const matchedVibes = [...(VIBE_ALIAS_GROUPS.get(alias) ?? [])];
    if (matchedVibes.length === 0) {
      continue;
    }

    matchedVibes.forEach((vibe) => vibes.add(vibe));
    vibeGroups.push(matchedVibes);
  }

  for (const guide of index.guides) {
    if (guide._aliases.some((alias) => consumePhrase(normalizedQuery, alias, consumedTokens))) {
      guideSlugs.add(guide.slug);
    }
  }

  if (options.scope === "guide") {
    guideSlugs.clear();
  }

  const unmatchedTerms = tokens.filter(
    (token) => !STOP_WORDS.has(token) && !consumedTokens.has(token),
  );

  return {
    categories,
    guideSlugs,
    normalizedQuery,
    tokens,
    unmatchedTerms,
    vibes,
    vibeGroups,
  };
}

function scoreEntry(entry, parsed) {
  const matchedSignals = [];
  let score = 0;
  let matchedCategoryCount = 0;
  let matchedUnmatchedTermCount = 0;
  const entryTags = normalizedList(entry.tags);
  const entryVibes = normalizedList(entry.vibe_tags);
  const categoryText = normalizeText([entry.category, ...entryTags].join(" "));
  const locationMatched = parsed.guideSlugs.has(entry.guide_slug);

  for (const category of parsed.categories) {
    const matchTerms = CATEGORY_MATCH_TERMS.get(category) || [category];
    if (matchTerms.some((term) => categoryText.includes(normalizeText(term)))) {
      score += 38;
      matchedCategoryCount += 1;
      matchedSignals.push("category");
    }
  }

  const reviewMatchesVibe = (vibe) =>
    (VIBE_ALIASES.get(vibe) || []).some((alias) =>
      containsPhrase(entry._reviewText, normalizeText(alias)),
    );

  for (const vibe of parsed.vibes) {
    if (entryVibes.includes(vibe) || entryTags.includes(vibe)) {
      score += 34;
      matchedSignals.push("vibe");
    } else if (reviewMatchesVibe(vibe)) {
      score += 4;
      matchedSignals.push("review");
    }
  }

  if (parsed.normalizedQuery && entry._name === parsed.normalizedQuery) {
    score += 55;
    matchedSignals.push("name");
  } else if (parsed.normalizedQuery && containsPhrase(entry._name, parsed.normalizedQuery)) {
    score += 36;
    matchedSignals.push("name");
  }

  if (parsed.normalizedQuery && containsPhrase(entry._guideTitle, parsed.normalizedQuery)) {
    score += 28;
    matchedSignals.push("guide");
  }

  if (parsed.normalizedQuery && containsPhrase(entry._city, parsed.normalizedQuery)) {
    score += 24;
    matchedSignals.push("city");
  }

  if (parsed.normalizedQuery && containsPhrase(entry._neighborhood, parsed.normalizedQuery)) {
    score += 18;
    matchedSignals.push("neighborhood");
  }

  if (parsed.normalizedQuery && containsPhrase(entry._note, parsed.normalizedQuery)) {
    score += 16;
    matchedSignals.push("text");
  }

  if (parsed.normalizedQuery && containsPhrase(entry._country, parsed.normalizedQuery)) {
    score += 12;
    matchedSignals.push("country");
  }

  if (
    parsed.normalizedQuery &&
    (entryTags.includes(normalizeToken(parsed.normalizedQuery)) ||
      entryVibes.includes(normalizeToken(parsed.normalizedQuery)) ||
      containsPhrase(categoryText, parsed.normalizedQuery))
  ) {
    score += 18;
    matchedSignals.push("tag");
  }

  if (parsed.normalizedQuery && containsPhrase(entry._searchText, parsed.normalizedQuery)) {
    score += 6;
    matchedSignals.push("text");
  }

  for (const term of parsed.unmatchedTerms) {
    const nameMatch = scoreTokenMatch(term, entry._nameTokens);
    const tagMatch = scoreTokenMatch(term, entry._tagTokens);
    const categoryMatch = scoreTokenMatch(term, entry._categoryTokens);
    const cityMatch = scoreTokenMatch(term, entry._cityTokens);
    const guideTitleMatch = scoreTokenMatch(term, entry._guideTitleTokens);
    const neighborhoodMatch = scoreTokenMatch(term, entry._neighborhoodTokens);
    const noteMatch = scoreTokenMatch(term, entry._noteTokens);
    const countryMatch = scoreTokenMatch(term, entry._countryTokens);
    const searchMatch = scoreTokenMatch(term, entry._searchTokens);
    let termMatched = false;

    if (nameMatch > 0) {
      score += nameMatch === 1 ? 16 : 12;
      matchedSignals.push("name");
      termMatched = true;
    } else if (
      tagMatch > 0 ||
      entryTags.includes(term) ||
      entryVibes.includes(term) ||
      categoryMatch > 0
    ) {
      score += 10;
      matchedSignals.push("tag");
      termMatched = true;
    } else if (cityMatch > 0 || guideTitleMatch > 0) {
      score += 8;
      matchedSignals.push("city");
      termMatched = true;
    } else if (neighborhoodMatch > 0) {
      score += 7;
      matchedSignals.push("neighborhood");
      termMatched = true;
    } else if (noteMatch > 0) {
      score += 5;
      matchedSignals.push("text");
      termMatched = true;
    } else if (countryMatch > 0) {
      score += 4;
      matchedSignals.push("country");
      termMatched = true;
    } else if (searchMatch > 0) {
      score += 1;
      matchedSignals.push("text");
      termMatched = true;
    }

    if (termMatched) {
      matchedUnmatchedTermCount += 1;
    }
  }

  if (parsed.tokens.length === 0) {
    score = 1;
  }

  const missingRequiredConstraint =
    (parsed.categories.size > 0 && matchedCategoryCount !== parsed.categories.size) ||
    parsed.vibeGroups.some(
      (group) =>
        !group.some(
          (vibe) =>
            entryVibes.includes(vibe) || entryTags.includes(vibe) || reviewMatchesVibe(vibe),
        ),
    ) ||
    (parsed.unmatchedTerms.length > 0 &&
      matchedUnmatchedTermCount !== parsed.unmatchedTerms.length);

  if (missingRequiredConstraint) {
    return {
      entry,
      matchedSignals: [],
      score: 0,
    };
  }

  const hasNonLocationIntent =
    parsed.categories.size > 0 || parsed.vibes.size > 0 || parsed.unmatchedTerms.length > 0;
  const hasNonLocationMatch = matchedSignals.length > 0;
  if (locationMatched && (!hasNonLocationIntent || hasNonLocationMatch)) {
    score += 80;
    matchedSignals.push("location");
  }

  const hasMatch = parsed.tokens.length === 0 || matchedSignals.length > 0;
  if (hasMatch && entry.top_pick) {
    score += 8;
    matchedSignals.push("top-pick");
  }
  if (hasMatch && Number(entry.manual_rank) > 0) {
    score += Math.min(8, Number(entry.manual_rank));
  }

  return {
    entry,
    matchedSignals: [...new Set(matchedSignals)],
    score,
  };
}

function guideAliases(guide) {
  const aliases = new Set([
    guide.slug,
    String(guide.slug || "").replaceAll("-", " "),
    guide.title,
    guide.city,
    guide.country,
    guide.country_code,
    [guide.city, guide.country].filter(Boolean).join(" "),
  ]);
  for (const alias of LOCATION_ALIASES.get(guide.slug) || []) {
    aliases.add(alias);
  }
  return [...aliases].map(normalizeText).filter(Boolean);
}

function tokenize(value) {
  return normalizeText(value)
    .split(" ")
    .filter((token) => token.length > 0);
}

function tokenizeList(values) {
  return [...new Set(values.flatMap((value) => tokenize(value)).filter(Boolean))];
}

function consumePhrase(query, phrase, consumedTokens) {
  const normalizedPhrase = normalizeText(phrase);
  if (!containsPhrase(query, normalizedPhrase)) {
    return false;
  }
  for (const token of tokenize(normalizedPhrase)) {
    consumedTokens.add(token);
  }
  return true;
}

function consumeDisjointPhrase(query, phrase, consumedTokens) {
  const normalizedPhrase = normalizeText(phrase);
  const phraseTokens = tokenize(normalizedPhrase);

  if (phraseTokens.length === 0 || phraseTokens.every((token) => consumedTokens.has(token))) {
    return false;
  }

  return consumePhrase(query, normalizedPhrase, consumedTokens);
}

function containsPhrase(query, phrase) {
  if (/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(phrase)) {
    return query.includes(phrase);
  }
  if (!phrase) {
    return false;
  }
  if (phrase.length <= 2) {
    return new RegExp(`(^|\\s)${escapeRegExp(phrase)}($|\\s)`).test(query);
  }
  return new RegExp(`(^|\\s)${escapeRegExp(phrase)}($|\\s)`).test(query);
}

function scoreTokenMatch(term, tokens) {
  if (!term || !Array.isArray(tokens) || tokens.length === 0) {
    return 0;
  }
  if (
    tokens.includes(term) ||
    (/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u.test(term) &&
      tokens.some((token) => token.includes(term)))
  ) {
    return 1;
  }
  if (term.length >= 2 && tokens.some((token) => token.startsWith(term))) {
    return 0.7;
  }
  return 0;
}

function normalizedList(values) {
  return values.map(normalizeToken).filter(Boolean);
}

function normalizeToken(value) {
  return normalizeText(value).replaceAll(" ", "-");
}

function curatedSort(left, right) {
  const topPickDelta = Number(Boolean(right.top_pick)) - Number(Boolean(left.top_pick));
  if (topPickDelta !== 0) {
    return topPickDelta;
  }
  return (
    Number(right.manual_rank || 0) - Number(left.manual_rank || 0) ||
    left.name.localeCompare(right.name)
  );
}

function prepareSearchIndexOnce(index) {
  if (index?.guideBySlug instanceof Map) {
    return index;
  }
  return prepareSearchIndex(index);
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
