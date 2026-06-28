"""Browser-backed scraper for individual Google Maps place pages."""

from __future__ import annotations

import json
import re
import statistics
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, unquote, urlparse

from gmaps_scraper.models import (
    PLACE_LLM_DISPLAY_TRANSLATION_FIELDS,
    PLACE_LLM_DOM_REPAIR_FIELDS,
    PLACE_LLM_REPAIR_FIELDS,
    AddressParts,
    PlaceAboutItem,
    PlaceAboutSection,
    PlaceDetails,
    PlaceExtractionDiagnostics,
    PlaceLLMRepairRequest,
    PlaceReservationLink,
    PlaceReview,
    PlaceScrapeResult,
    ReviewTopic,
)
from gmaps_scraper.scraper import (
    _HTTP_IMPERSONATE,
    BrowserSessionConfig,
    HttpSessionConfig,
    ScrapeError,
    _extract_preloaded_fetch_url,
    _handle_google_consent,
    _import_curl_requests,
    _launch_browser_context,
    _load_http_cookie_jar,
    _normalize_response_url,
    _raise_for_status,
    _response_text,
    _save_http_cookie_jar,
)
from gmaps_scraper.translation_memory import TranslationMemory, needs_display_en
from gmaps_scraper.url_tools import extract_list_id

_TITLE_SELECTORS = ("h1.DUwDvf", "h1.lfPIob", "div[role='main'] h1")
_TITLE_SELECTOR = ", ".join(_TITLE_SELECTORS)
_PLACE_LLM_PROMPT_VERSION = "gmaps-place-repair-v1"
_TRANSLATION_MEMORY = TranslationMemory.default()
type PlaceLLMRepairer = Callable[[PlaceLLMRepairRequest], Mapping[str, object] | None]
type PlaceLLMTask = Literal["dom_repair", "display_translation"]
_DEFAULT_LLM_TASKS: tuple[PlaceLLMTask, ...] = ("dom_repair", "display_translation")
_DOM_REPAIR_QUALITY_FLAGS = {
    "address_rejected",
    "limited_view",
    "missing_name",
    "name_rejected",
    "search_result_page",
    "thin_place_result",
}
_DISPLAY_TRANSLATION_QUALITY_FLAGS = {
    "needs_address_display_en",
    "needs_category_display_en",
}
_REVIEW_LABEL_KEYWORDS = ("review", "reviews", "評論", "クチコミ")
_REVIEW_TOPIC_REJECT_LABELS = {
    "all",
    "all reviews",
    "highest",
    "like",
    "likes",
    "lowest",
    "most relevant",
    "newest",
    "review",
    "reviews",
    "search",
    "sort",
    "write a review",
}
_REVIEW_TOPIC_REJECT_TERMS = (
    "all reviews",
    "google reviews",
    "highest rated",
    "lowest rated",
    "most relevant",
    "newest",
    "review summary",
    "sort by",
    "write a review",
)
_UI_ACTION_LABELS = {
    "call",
    "directions",
    "save",
    "saved",
    "share",
    "website",
}
_UI_ACTION_CLUSTER_LABEL_PATTERN = (
    r"(?:call|directions|save|saved|nearby|send to phone|share|website|sign in)"
)
_UI_ACTION_CLUSTER_RE = re.compile(
    rf"^{_UI_ACTION_CLUSTER_LABEL_PATTERN}(?:\s+{_UI_ACTION_CLUSTER_LABEL_PATTERN}){{2,}}$",
    re.IGNORECASE,
)
_DESCRIPTION_STOP_MARKERS = {
    "photos",
    "about this data",
    "sponsored",
    "write a review",
    "claim this business",
    "suggest an edit",
    "overview reviews about",
    "limited view of google maps",
    "get the most out of google maps",
    "our policies do not permit contributions to this type of place.",
}
_DESCRIPTION_STOP_MARKER_KEYS = {
    marker.casefold().strip(" .") for marker in _DESCRIPTION_STOP_MARKERS
}
_DESCRIPTION_STOP_SUBSTRINGS = (
    "mark as temporarily closed",
    "remove this place",
    "report a legal problem",
)
_DESCRIPTION_SERVICE_OPTION_SEGMENT_PATTERN = re.compile(
    r"^(?:[✓✔☑✗✕✖\ue5ca\ue5cb\ue5cc\ue5cd\ue5cf]\s*)?"
    r"(?:dine-?in|takeout|delivery|curbside pickup|kerbside pickup|"
    r"no-contact delivery|drive-through|drive thru|takeaway|reservations?|"
    r"dogs allowed|onsite services?)$",
    re.IGNORECASE,
)
_DESCRIPTION_REVIEW_RESPONSE_MARKERS = (
    "thank you for",
    "we're thrilled",
    "we apologize",
    "ended up going",
    "my research",
    "my son and i",
    "i had high hopes",
    "i'm sorry to inform",
)
_DESCRIPTION_REVIEW_PROSE_MARKERS = (
    "best place to stay",
    "boy was it worth",
    "definitely recommend this place",
    "great experience overall",
    "had a great time",
    "hidden gem-literally",
    "highly recommended",
    "i forgot his name",
    "i'd just finished",
    "i've tasted",
    "i’d just finished",
    "i’ve tasted",
    "it was my first attempt",
    "my stay in",
    "once step in",
    "offered great recommendation",
    "omfg",
    "overrated",
    "so yummy",
    "the katsu burger",
    "the rooms were huge",
    "we have ever had",
    "we've ever had",
    "what a great hotel",
    "would recommend to everyone",
    "about this data",
    "get the most out of google maps",
    "your children",
    "your kids",
    "you should",
)
_DESCRIPTION_FIRST_PERSON_PRONOUN_PATTERN = re.compile(
    r"\b(?:[Ii]|[Ii]['’](?:m|d|ve)|[Mm]y|[Mm]e|[Ww]e|[Ww]e['’](?:re|d|ve)|[Oo]ur|[Uu]s)\b",
)
_DESCRIPTION_FIRST_PERSON_EXPERIENCE_MARKERS = (
    "visited",
    "attended",
    "arrived",
    "ordered",
    "enjoyed",
    "stopped by",
    "spent",
    "tried",
    "came",
    "went",
    "found",
    "felt",
    "hit",
    "think",
    "saw",
    "seen",
    "chose",
    "celebrate",
    "celebrated",
    "return",
    "returned",
)
_DESCRIPTION_FIRST_PERSON_EXPERIENCE_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(re.escape(marker) for marker in _DESCRIPTION_FIRST_PERSON_EXPERIENCE_MARKERS)
    + r")\b",
    re.IGNORECASE,
)
_SEARCH_RESULTS_LABELS = {
    "result",
    "results",
    "search result",
    "search results",
    "共有",
    "結果",
}
_CATEGORY_SUFFIX_PATTERN = re.compile(
    r"\b("
    r"restaurant|cafe|coffee shop|bar|bakery|hotel|lodging|museum|park|station|"
    r"store|shop|supermarket|market|mall|school|university|gym|spa|clinic|"
    r"hospital|pharmacy|library|church|temple|shrine|tourist attraction|"
    r"movie theater|fast food restaurant|ramen restaurant|sushi restaurant"
    r")\b$",
    re.IGNORECASE,
)
_ADMISSION_CONTEXT_PATTERN = re.compile(
    r"\b(?:admission|ticket|tickets|entry fee|entrance fee)\b|入場|入園|票價|票价|門票|门票",
    re.IGNORECASE,
)
_PLUS_CODE_PATTERN = re.compile(
    r"\b[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}"
    r"(?:\s+[^\n]+)?\b"
)
_PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9()\-\s]{7,}$")
_STATUS_LINE_PATTERN = re.compile(
    r"^(?:"
    r"(?:temporarily|permanently)\s+closed\b"
    r"|(?:opens|closes)\b"
    r"|(?:open|closed)\s+now(?:\s*$|\s*(?:[·⋅]|[-–—])\s*(?:opens?|closes?)\b)"
    r"|(?:open|closed)\s+24\s*hours\b"
    r"|(?:open|closed)\s*(?:[·⋅]|[-–—])\s*(?:opens?|closes?)\b"
    r")",
    re.IGNORECASE,
)
_POSTAL_CODE_PATTERN = re.compile(
    r"\b(?:\d{5}(?:-\d{4})?|[A-Z]\d[A-Z]\s?\d[A-Z]\d|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2})\b",
    re.IGNORECASE,
)
_ADDRESS_KEYWORD_PATTERN = re.compile(
    r"\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|place|pl|"
    r"court|ct|square|sq|suite|ste|unit|floor|fl|plaza|parkway|pkwy|highway|hwy)\b",
    re.IGNORECASE,
)
_STRONG_ADDRESS_KEYWORD_PATTERN = re.compile(
    r"\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|"
    r"square|sq|suite|ste|unit|floor|fl|plaza|parkway|pkwy|highway|hwy)\b",
    re.IGNORECASE,
)
# These reject lists only apply after structured DOM extraction misses and we
# are forced to classify plain Google Maps text rows. They intentionally target
# UI/review vocabulary that commonly appears next to real address rows.
_PROSE_TERM_PATTERN = re.compile(
    r"\b(?:best|good|great|delicious|dropped|experience|lunch|dinner|"
    r"burger|burgers|coffee|food|friendly|nugget|nuggets|owner|recommend|recommended|session)\b",
    re.IGNORECASE,
)
_ADDRESS_REJECT_SUBSTRINGS = (
    "about this data",
    "faviconv2",
    "imagery ©",
    "imagery©",
    "map data ©",
    "map data©",
    "saved in",
    "send product feedback",
    "street view",
    "termsprivacy",
)
_LOCALITY_ADDRESS_REJECT_VALUES = {
    "art gallery",
    "bakery",
    "bar",
    "cafe",
    "coffee shop",
    "curbside pickup",
    "delivery",
    "dessert shop",
    "dine-in",
    "dine in",
    "drive-through",
    "drive through",
    "hotel",
    "ice cream shop",
    "kerbside pickup",
    "museum",
    "no-contact delivery",
    "outdoor seating",
    "reservations",
    "restaurant",
    "shop",
    "shopping mall",
    "store",
    "takeaway",
    "takeout",
    "tourist attraction",
    "wheelchair accessible entrance",
}
_ADDRESS_REJECT_HOST_FRAGMENTS = ("gstatic.com", "googleusercontent.com")
_ADDRESS_ENTITY_TOKEN_PATTERN = re.compile(r"^/(?:g|m)/[A-Za-z0-9_-]+$")
_URL_LIKE_PATTERN = re.compile(
    r"(?:https?://|www\.|/(?:search|maps|url|local)(?:[/?#]|$))",
    re.IGNORECASE,
)
_TRAVEL_PRODUCT_ADDRESS_PATTERN = re.compile(r"\b\d+\s*[-–]\s*day\b", re.IGNORECASE)
# Locality-only addresses can legitimately contain periods in abbreviations
# like "St. Louis" or "D.C."; prose with arbitrary periods is rejected later.
_LOCALITY_ABBREVIATION_PERIOD_PATTERN = re.compile(r"(?:\bSt\.|\b[A-Z]\.(?:[A-Z]\.)+)")
_PRICE_RANGE_PATTERN = re.compile(
    r"(?<!\S)(?:"
    r"\${1,4}|"
    r"(?:[$€£¥₩₹₫฿₱₦₺₴₽]|SGD|USD|EUR|GBP|JPY|TWD|NT\$|HK\$|CA\$|A\$)"
    r"\s*[0-9][0-9,.\s\u00a0]*(?:\+|[-–]\s*"
    r"(?:[$€£¥₩₹₫฿₱₦₺₴₽]|SGD|USD|EUR|GBP|JPY|TWD|NT\$|HK\$|CA\$|A\$)?"
    r"\s*[0-9][0-9,.\s\u00a0]*)?"
    r")(?=\s|$|·)",
    flags=re.IGNORECASE,
)
_NUMERIC_PRICE_PATTERN = re.compile(
    r"(?<!\S)(?P<currency>"
    r"(?:[$€£¥₩₹₫฿₱₦₺₴₽]|SGD|USD|EUR|GBP|JPY|TWD|NT\$|HK\$|CA\$|A\$)"
    r")\s*(?P<amount>[0-9][0-9,.\s\u00a0]*)(?=\s|$|·)",
    flags=re.IGNORECASE,
)
_PLACE_PANEL_HELPERS_JS = r"""
  const cleanLine = (value) => (value || "").replace(/\s+/g, " ").trim();
  const searchResultTitleLabels = new Set([
    "result",
    "results",
    "search result",
    "search results",
    "結果",
    "検索結果",
  ]);
  const titleSelectors = ["h1.DUwDvf", "h1.lfPIob", "div[role='main'] h1"];
  const isSearchResultTitle = (value) => searchResultTitleLabels.has(
    cleanLine(value).toLowerCase(),
  );
  const visibleRect = (element) => {
    const rect = element.getBoundingClientRect();
    const width = Math.max(0, Math.min(rect.right, window.innerWidth) - Math.max(rect.left, 0));
    const height = Math.max(0, Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0));
    return {rect, visibleArea: width * height};
  };
  const matchingTitleElements = (root) => {
    const elements = [];
    const seen = new Set();
    for (const selector of titleSelectors) {
      const matches = [];
      if (root.matches?.(selector)) {
        matches.push(root);
      }
      matches.push(...root.querySelectorAll(selector));
      for (const element of matches) {
        if (seen.has(element)) {
          continue;
        }
        seen.add(element);
        elements.push(element);
      }
    }
    return elements;
  };
  const placePanelCandidateRoots = () => {
    const roots = new Set();
    for (const selector of [
      "[role='main']",
      "[role='dialog']",
      "[role='region'][aria-label]",
    ]) {
      for (const element of document.querySelectorAll(selector)) {
        roots.add(element);
      }
    }
    for (const title of document.querySelectorAll("h1, [role='heading']")) {
      let current = title;
      for (let depth = 0; depth < 9 && current && current !== document.body; depth += 1) {
        roots.add(current);
        current = current.parentElement;
      }
    }
    return Array.from(roots);
  };
  const placePanelRoot = () => {
    let best = null;
    for (const root of placePanelCandidateRoots()) {
      const {rect, visibleArea} = visibleRect(root);
      if (visibleArea <= 0 || rect.width < 120 || rect.height < 80) {
        continue;
      }
      const titleElements = matchingTitleElements(root);
      const titleTexts = titleElements
        .map((element) => cleanLine(element.innerText || element.textContent || ""))
        .filter(Boolean);
      const titleElement = titleElements.find((element) => {
        const title = cleanLine(element.innerText || element.textContent || "");
        return title && !isSearchResultTitle(title);
      }) || titleElements.find(
        (element) => cleanLine(element.innerText || element.textContent || ""),
      );
      const title = cleanLine(titleElement?.innerText || titleElement?.textContent || "");
      const nonResultTitle = title && !isSearchResultTitle(title) ? title : "";
      const label = cleanLine(root.getAttribute("aria-label") || "");
      const hasResultsTitle = titleTexts.some(isSearchResultTitle) || /results for/i.test(label);
      const addressRows = root.querySelectorAll(`[data-item-id="address"]`).length;
      const ratingSummaries = root.querySelectorAll("div.F7nice").length;
      const tabLabels = Array.from(
        root.querySelectorAll("button[role='tab'], div[role='tablist'] button"),
      ).map((element) => cleanLine(element.getAttribute("aria-label") || element.innerText || ""));
      const hasOverviewTabs = tabLabels.some((value) => /overview/i.test(value))
        && tabLabels.some((value) => /reviews?/i.test(value));
      const articleCount = root.querySelectorAll("[role='article']").length;
      const placeRows = root.querySelectorAll(
        `[data-item-id="address"], [data-item-id="authority"], [data-item-id^="phone:"]`,
      ).length;
      let score = 0;
      if (nonResultTitle) {
        score += 120;
      }
      if (addressRows) {
        score += 45;
      }
      if (ratingSummaries) {
        score += 30;
      }
      if (hasOverviewTabs) {
        score += 25;
      }
      score += Math.min(placeRows * 10, 30);
      if (root.getAttribute("role") === "main" && nonResultTitle) {
        score += 10;
      }
      if (label && nonResultTitle && label === nonResultTitle) {
        score += 8;
      }
      if (hasResultsTitle) {
        score -= 70;
      }
      if (articleCount >= 2 && !addressRows) {
        score -= 40;
      }
      score += Math.min(visibleArea / 20000, 20);
      const tieBreak = (root.getAttribute("role") === "main" ? 2000000 : 0) + visibleArea;
      if (!best || score > best.score || (score === best.score && tieBreak > best.tieBreak)) {
        best = {root, titleElement, title, score, tieBreak};
      }
    }
    if (best && best.score > 0) {
      return {
        root: best.root,
        titleElement: best.titleElement,
        title: best.title,
        found: true,
        score: best.score,
      };
    }
    const fallbackTitle = document.querySelector(titleSelectors.join(","));
    return {
      root: document.body,
      titleElement: fallbackTitle,
      title: cleanLine(fallbackTitle?.innerText || fallbackTitle?.textContent || ""),
      found: false,
      score: 0,
    };
  };
"""
_PLACE_JS_EXTRACTOR = r"""
() => {
""" + _PLACE_PANEL_HELPERS_JS + r"""

  const panelInfo = placePanelRoot();
  const panel = panelInfo.root;
  const titleElement = panelInfo.titleElement;

  const firstText = (selectors, root = panel) => {
    for (const selector of selectors) {
      const element = root.querySelector(selector);
      const text = element?.innerText?.trim();
      if (text) {
        return text;
      }
    }
    return null;
  };

  const firstAttr = (selectors, attr, root = panel) => {
    for (const selector of selectors) {
      const element = root.querySelector(selector);
      const value = element?.getAttribute(attr)?.trim();
      if (value) {
        return value;
      }
    }
    return null;
  };

  const isReviewScoped = (element) => {
    if (!element) {
      return false;
    }
    if (element.closest("[data-review-id]")) {
      return true;
    }
    const label = element.getAttribute?.("aria-label") || "";
    return /(^|\W)reviews?(\W|$)/i.test(label);
  };

  const firstImageUrl = (selectors, root = panel) => {
    for (const selector of selectors) {
      for (const element of root.querySelectorAll(selector)) {
        if (isReviewScoped(element)) {
          continue;
        }
        const value = element?.currentSrc
          || element?.getAttribute("src")?.trim()
          || element?.getAttribute("data-src")?.trim();
        if (value) {
          return value;
        }
      }
    }
    return null;
  };

  const firstBackgroundImageUrl = (selectors, root = panel) => {
    for (const selector of selectors) {
      for (const element of root.querySelectorAll(selector)) {
        if (isReviewScoped(element)) {
          continue;
        }
        const style = getComputedStyle(element).backgroundImage || "";
        const match = style.match(/url\((['"]?)(.*?)\1\)/);
        if (match?.[2]) {
          return match[2].trim();
        }
      }
    }
    return null;
  };

  const itemValue = (itemId) => firstText([
    `[data-item-id="${itemId}"] .Io6YTe`,
    `[data-item-id="${itemId}"]`,
  ]);

  const rowValue = (row) => {
    // `.DkEaL` can be a localized row label when the value is in `.Io6YTe`.
    // Prefer the value node and only use `.DkEaL` for older rows where it is
    // the address text itself.
    const value = (
      row?.querySelector(".Io6YTe")?.innerText?.trim()
      || row?.querySelector(".DkEaL")?.innerText?.trim()
    );
    return value || null;
  };

  const isAddressIcon = (icon) => {
    const label = icon?.getAttribute?.("aria-label") || "";
    const glyph = icon?.innerText?.trim() || icon?.textContent?.trim() || "";
    return label === "Address" || glyph === "";
  };

  const addressValue = () => {
    // Prefer Google Maps' structured address row. The icon fallback exists for
    // localized pages where the aria-label text changes but the address glyph
    // and row shape remain stable.
    const legacy = itemValue("address");
    if (legacy) {
      return legacy;
    }
    for (const icon of panel.querySelectorAll(".google-symbols, [role='img']")) {
      if (!isAddressIcon(icon)) {
        continue;
      }
      const row = icon.closest(".LCF4w, .MngOvd, .RcCsl, [data-section-id]");
      const value = rowValue(row);
      if (value && value !== "Address") {
        return value;
      }
    }
    return null;
  };
  const addressRowElement = () => {
    const legacy = panel.querySelector(`[data-item-id="address"]`);
    if (legacy) {
      return legacy;
    }
    for (const icon of panel.querySelectorAll(".google-symbols, [role='img']")) {
      if (!isAddressIcon(icon)) {
        continue;
      }
      const row = icon.closest(".LCF4w, .MngOvd, .RcCsl, [data-section-id]");
      if (row) {
        return row;
      }
    }
    return null;
  };
  const elementTop = (element) => {
    const rect = element?.getBoundingClientRect?.();
    return rect && rect.height > 0 ? rect.top : null;
  };
  const elementBottom = (element) => {
    const rect = element?.getBoundingClientRect?.();
    return rect && rect.height > 0 ? rect.bottom : null;
  };
  const descriptionBoundaryTop = () => {
    const rows = Array.from(panel.querySelectorAll("[data-item-id]"))
      .map(elementTop)
      .filter((value) => value !== null);
    if (rows.length > 0) {
      return Math.min(...rows);
    }
    const addressRow = addressRowElement();
    const addressTop = elementTop(addressRow);
    return addressTop === null ? Infinity : addressTop;
  };
  const descriptionValue = () => {
    const direct = firstText([".WeS02d", ".PYvSYb"]);
    if (direct) {
      return direct;
    }
    const titleBottom = Math.max(
      ...[
        elementBottom(titleElement),
        ...Array.from(panel.querySelectorAll("div.F7nice")).map(elementBottom),
      ].filter((value) => value !== null),
      0,
    );
    const boundaryTop = descriptionBoundaryTop();
    const candidates = [];
    for (const element of panel.querySelectorAll("div, span")) {
      const text = cleanLine(element.innerText || element.textContent || "");
      if (!text || text.includes("·")) {
        continue;
      }
      if (
        element.closest(
          "button, a, [role='button'], [role='tab'], [role='tablist'], "
            + "[data-item-id], [data-review-id], div.F7nice",
        )
      ) {
        continue;
      }
      if (
        Array.from(element.children).some(
          (child) => cleanLine(child.innerText || child.textContent || "") === text,
        )
      ) {
        continue;
      }
      const top = elementTop(element);
      if (top === null || top <= titleBottom || top >= boundaryTop) {
        continue;
      }
      candidates.push({top, text});
    }
    candidates.sort((left, right) => left.top - right.top);
    return candidates[0]?.text || null;
  };

  const normalizeCount = (value) => {
    if (!value) {
      return 0;
    }
    const text = value.trim().toUpperCase();
    let multiplier = 1;
    if (text.includes("K")) {
      multiplier = 1000;
    } else if (text.includes("M")) {
      multiplier = 1000000;
    } else if (text.includes("萬") || text.includes("万")) {
      multiplier = 10000;
    }
    const numeric = parseFloat(text.replace(/[,\sKM萬万]/g, ""));
    return Number.isFinite(numeric) ? numeric * multiplier : 0;
  };

  const reviewKeywords = ["review", "reviews", "評論", "クチコミ"];
  const reviewCountPattern = new RegExp(
    "([0-9][0-9,.\\s]*[KM萬万]?)[ ]*"
      + "(?:reviews?|評論|クチコミ|件のクチコミ|件の Google クチコミ|則評論|篇評論)",
    "i",
  );
  const reviewCountPatternReverse = new RegExp(
    "(?:reviews?|評論|クチコミ)\\s*[(]([0-9][0-9,.\\s]*[KM萬万]?)[)]",
    "i",
  );

  let reviewCount = null;
  let reviewSource = null;
  let bestCount = 0;

  const considerCount = (candidate, source) => {
    if (!candidate) {
      return;
    }
    const count = normalizeCount(candidate);
    if (count <= 0) {
      return;
    }
    if (count > bestCount) {
      bestCount = count;
      reviewCount = candidate.trim();
      reviewSource = source;
    }
  };

  for (const span of panel.querySelectorAll("div.F7nice span")) {
    const text = span.innerText?.trim() || "";
    const match = text.match(/^\(?([0-9][0-9,.\s]*[KM萬万]?)\)?$/i);
    if (!match) {
      continue;
    }
    if (/^[0-9]+([.,][0-9]+)?$/.test(match[1]) && normalizeCount(match[1]) < 10) {
      continue;
    }
    considerCount(match[1], "f7nice");
  }

  const reviewSummaryBoundaryTop = () => {
    const addressRow = addressRowElement();
    if (addressRow) {
      const rect = addressRow.getBoundingClientRect();
      if (rect.height > 0) {
        return rect.top;
      }
    }
    const panelRect = panel.getBoundingClientRect();
    return panelRect.top + 520;
  };
  const isOverviewReviewCountElement = (element) => {
    if (element.closest("div.F7nice")) {
      return true;
    }
    const rect = element.getBoundingClientRect();
    if (rect.height <= 0) {
      return false;
    }
    return rect.top < reviewSummaryBoundaryTop();
  };

  for (const element of panel.querySelectorAll("[aria-label]")) {
    if (!isOverviewReviewCountElement(element)) {
      continue;
    }
    const label = element.getAttribute("aria-label") || "";
    if (!reviewKeywords.some((keyword) => label.toLowerCase().includes(keyword.toLowerCase()))) {
      continue;
    }
    const match = label.match(reviewCountPattern) || label.match(reviewCountPatternReverse);
    if (match) {
      considerCount(match[1], "aria-label");
    }
  }

  if (!reviewCount) {
    for (const tab of panel.querySelectorAll("div[role='tablist'] button")) {
      const text = tab.innerText?.trim() || "";
      if (!reviewKeywords.some((keyword) => text.toLowerCase().includes(keyword.toLowerCase()))) {
        continue;
      }
      const match = text.match(/([0-9][0-9,.\s]*[KM萬万]?)/i);
      if (match) {
        considerCount(match[1], "tab");
      }
    }
  }

  const mainPhotoUrl = firstImageUrl([
    "div.RZ66Rb button[jsaction*='heroHeaderImage'] img",
    "button[jsaction*='heroHeaderImage'] img",
    "div.ZKCDEc [data-photo-index='0'] img",
    "[data-photo-index='0'] img",
    "[data-photo-index] img",
  ])
    || firstBackgroundImageUrl([
      "div.RZ66Rb button[jsaction*='heroHeaderImage']",
      "button[jsaction*='heroHeaderImage']",
      "div.ZKCDEc [data-photo-index='0']",
      "[data-photo-index='0']",
      "[data-photo-index]",
    ]);
  const photoUrl = mainPhotoUrl
    || firstAttr(["meta[property='og:image']", "meta[itemprop='image']"], "content", document);

  const shallowPath = (element) => {
    const parts = [];
    let current = element;
    for (let i = 0; i < 4 && current && current.nodeType === Node.ELEMENT_NODE; i += 1) {
      let part = current.tagName.toLowerCase();
      const id = current.getAttribute("data-item-id");
      const role = current.getAttribute("role");
      if (id) {
        part += `[data-item-id="${id}"]`;
      } else if (role) {
        part += `[role="${role}"]`;
      } else if (current.classList?.length) {
        part += "." + Array.from(current.classList).slice(0, 2).join(".");
      }
      parts.unshift(part);
      current = current.parentElement;
    }
    return parts.join(" > ");
  };
  const nearbyText = (element) => {
    const texts = [];
    const parent = element.parentElement;
    if (!parent) {
      return texts;
    }
    for (const child of parent.children) {
      const text = cleanLine(child.innerText || child.textContent || "");
      if (text && !texts.includes(text)) {
        texts.push(text);
      }
      if (texts.length >= 4) {
        break;
      }
    }
    return texts;
  };
  const collectDomCandidates = () => {
    const selectors = [
      "[data-item-id]",
      "button[aria-label]",
      "a[aria-label]",
      "[role='button'][aria-label]",
      ".Io6YTe",
      ".DkEaL",
      ".F7nice",
      "div[role='tablist'] button",
    ];
    const candidates = [];
    const seen = new Set();
    for (const selector of selectors) {
      for (const element of panel.querySelectorAll(selector)) {
        const text = cleanLine(element.innerText || element.textContent || "");
        const ariaLabel = cleanLine(element.getAttribute("aria-label") || "");
        const dataItemId = cleanLine(element.getAttribute("data-item-id") || "");
        if (!text && !ariaLabel && !dataItemId) {
          continue;
        }
        if (text.length > 240 || ariaLabel.length > 240) {
          continue;
        }
        const key = `${selector}\n${text}\n${ariaLabel}\n${dataItemId}`;
        if (seen.has(key)) {
          continue;
        }
        seen.add(key);
        candidates.push({
          text,
          tag: element.tagName.toLowerCase(),
          role: cleanLine(element.getAttribute("role") || ""),
          aria_label: ariaLabel,
          data_item_id: dataItemId,
          selector_hint: shallowPath(element),
          nearby_text: nearbyText(element),
        });
        if (candidates.length >= 120) {
          return candidates;
        }
      }
    }
    return candidates;
  };
  const collectReviewTopics = () => {
    const selectors = [
      "button[jsaction*='review']",
      "button[aria-label*='review' i]",
      "button[role='radio']",
      "button[aria-pressed]",
      "div[role='button'][aria-label]",
    ];
    const topics = [];
    const seen = new Set();
    for (const selector of selectors) {
      for (const element of panel.querySelectorAll(selector)) {
        const text = cleanLine(element.innerText || element.textContent || "");
        const ariaLabel = cleanLine(element.getAttribute("aria-label") || "");
        const candidate = /[0-9]/.test(text)
          ? text
          : (/[0-9]/.test(ariaLabel) ? ariaLabel : text || ariaLabel);
        if (!candidate || candidate.length > 120 || !/[0-9]/.test(candidate)) {
          continue;
        }
        const key = `${candidate}\n${ariaLabel}`;
        if (seen.has(key)) {
          continue;
        }
        seen.add(key);
        topics.push({
          text: candidate,
          aria_label: ariaLabel,
          source: selector,
        });
      }
    }
    return topics;
  };
  const priceSymbols = "(?:[$€£¥₩₹₫฿₱₦₺₴₽]|SGD|USD|EUR|GBP|JPY|TWD|NT\\$|HK\\$|CA\\$|A\\$)";
  const pricePattern = new RegExp(
    "(?:^|\\s|·)((?:\\${1,4})|" + priceSymbols
      + "\\s*[0-9][0-9,.\u00a0\\s]*(?:\\+|[-–]\\s*" + priceSymbols
      + "?\\s*[0-9][0-9,.\u00a0\\s]*)?)",
    "i",
  );
  const exactNumericPricePattern = new RegExp(
    "^" + priceSymbols + "\\s*[0-9][0-9,.\u00a0\\s]*$",
    "i",
  );
  const extractPrice = (value) => {
    const text = cleanLine(value);
    const match = text.match(pricePattern);
    if (!match?.[1]) {
      return null;
    }
    return cleanLine(match[1].replace(/\u00a0/g, " "));
  };
  const looksLikePriceRangeText = (value) => {
    const text = cleanLine(value);
    if (!text) {
      return false;
    }
    if (/^\${1,4}$/.test(text)) {
      return true;
    }
    return text.includes("·") && pricePattern.test(text);
  };
  const headingAliases = (value) => Array.isArray(value) ? value : [value];
  const normalizedHeading = (value) => cleanLine(value).toLowerCase();
  const sectionRootByHeading = (headingText) => {
    const aliases = headingAliases(headingText)
      .map((value) => normalizedHeading(value))
      .filter(Boolean);
    if (aliases.length === 0) {
      return null;
    }
    for (const heading of panel.querySelectorAll("h2, h3, [role='heading']")) {
      const text = normalizedHeading(heading.innerText || heading.textContent || "");
      if (!aliases.includes(text)) {
        continue;
      }
      return (
        heading.closest(".m6QErb, section, [role='region'], [data-section-id]")
        || heading.parentElement
        || null
      );
    }
    return null;
  };
  const collectLeafPrices = (root) => {
    if (!root) {
      return [];
    }
    const prices = [];
    const seen = new Set();
    for (const element of root.querySelectorAll("*")) {
      const text = cleanLine(element.innerText || element.textContent || "");
      if (!text || text.length > 48) {
        continue;
      }
      if (
        Array.from(element.children).some(
          (child) => cleanLine(child.innerText || child.textContent || "") === text,
        )
      ) {
        continue;
      }
      const price = extractPrice(text);
      if (!price || price !== text || !exactNumericPricePattern.test(price)) {
        continue;
      }
      if (seen.has(price)) {
        continue;
      }
      seen.add(price);
      prices.push(price);
    }
    return prices;
  };
  const providerLabelFromUrl = (href) => {
    try {
      const host = new URL(href).hostname.replace(/^www\./, "");
      const base = host.split(".")[0] || host;
      return base
        .replace(/[-_]+/g, " ")
        .replace(/\b\w/g, (char) => char.toUpperCase());
    } catch {
      return "Find a Table";
    }
  };
  const reservationLabel = (element, href) => {
    const actionPrefixPattern = new RegExp(
      "^(?:find a table|reserve|make a reservation|book a table)"
        + "(?:\\s+(?:with|on|at|via))?\\s*",
      "i",
    );
    const raw = cleanLine(
      element.innerText
      || element.textContent
      || element.getAttribute("aria-label")
      || element.getAttribute("title")
      || "",
    );
    const cleaned = raw
      .replace(actionPrefixPattern, "")
      .replace(/\s+(?:opens in new tab|website)$/i, "")
      .trim();
    return cleaned || providerLabelFromUrl(href);
  };
  const collectReservationLinks = () => {
    const links = [];
    const seen = new Set();
    const reservationPattern = new RegExp(
      String.raw`\b(find a table|reserve|reservation|book a table)\b`,
      "i",
    );
    const providerHostPattern = new RegExp(
      "(opentable|resy|sevenrooms|thefork|tock|quandoo|yelp|inline|"
        + "tablecheck|exploretock|omakase|pocket-concierge|pocketconcierge|"
        + "tabelog|hotpepper|gnavi|gurunavi|ikyu|jpneazy|byfood|autoreserve)",
      "i",
    );
    for (const element of panel.querySelectorAll("a[href]")) {
      const href = element.href || element.getAttribute("href") || "";
      if (!/^https?:\/\//i.test(href) || seen.has(href)) {
        continue;
      }
      const evidence = [
        element.innerText,
        element.textContent,
        element.getAttribute("aria-label"),
        element.getAttribute("title"),
        element.getAttribute("data-item-id"),
        href,
      ].filter(Boolean).join(" ");
      if (!reservationPattern.test(evidence) && !providerHostPattern.test(evidence)) {
        continue;
      }
      seen.add(href);
      links.push({
        label: reservationLabel(element, href),
        url: href,
      });
      if (links.length >= 8) {
        break;
      }
    }
    return links;
  };
  const roomOverlayPrice = () => {
    const selectors = [
      ".rlmNhf button[aria-label]",
      "button[aria-label*='per night' i]",
      "button[aria-label*='prices from' i]",
    ];
    for (const selector of selectors) {
      for (const element of document.querySelectorAll(selector)) {
        const price = extractPrice(element.getAttribute("aria-label") || "");
        if (price && exactNumericPricePattern.test(price)) {
          return price;
        }
      }
    }
    return null;
  };
  const detailsBoundaryTop = () => {
    const selectors = [
      `[data-item-id="address"]`,
      `[data-item-id="authority"]`,
      `[data-item-id="oloc"]`,
      `[data-item-id="locatedin"]`,
      `button[data-item-id^="phone:"]`,
    ];
    let boundary = Number.POSITIVE_INFINITY;
    for (const selector of selectors) {
      for (const element of panel.querySelectorAll(selector)) {
        const rect = element.getBoundingClientRect();
        if (rect.height <= 0) {
          continue;
        }
        boundary = Math.min(boundary, rect.top);
      }
    }
    const addressRow = addressRowElement();
    if (addressRow) {
      const rect = addressRow.getBoundingClientRect();
      if (rect.height > 0) {
        boundary = Math.min(boundary, rect.top);
      }
    }
    return Number.isFinite(boundary) ? boundary : Number.POSITIVE_INFINITY;
  };
  const structuralOfferSignals = () => {
    const titleTop = (
      titleElement?.getBoundingClientRect()?.top
      || panel.getBoundingClientRect().top
    );
    const boundaryTop = detailsBoundaryTop();
    const prices = [];
    const seenPrices = new Set();
    for (const element of panel.querySelectorAll("*")) {
      const text = cleanLine(element.innerText || element.textContent || "");
      if (!text || text.length > 48) {
        continue;
      }
      const rect = element.getBoundingClientRect();
      if (rect.height <= 0 || rect.top <= titleTop || rect.top >= boundaryTop) {
        continue;
      }
      if (
        Array.from(element.children).some(
          (child) => cleanLine(child.innerText || child.textContent || "") === text,
        )
      ) {
        continue;
      }
      const price = extractPrice(text);
      if (!price || price !== text || !exactNumericPricePattern.test(price)) {
        continue;
      }
      if (seenPrices.has(price)) {
        continue;
      }
      seenPrices.add(price);
      prices.push(price);
    }
    const kind = (
      prices.length > 0
      ? (
        roomOverlayPrice()
        || panel.querySelector(`[data-item-id="place-info-links:"]`)
          ? "room"
          : "admission"
      )
      : null
    );
    return {kind, prices};
  };
  const priceRangeValue = () => {
    const roots = [
      panel.querySelector(".dmRWX"),
      panel.querySelector(".F7nice")?.parentElement,
      panel.querySelector(".F7nice"),
      panel,
    ].filter(Boolean);
    for (const root of roots) {
      const text = cleanLine(root.innerText || root.textContent || "");
      if (!looksLikePriceRangeText(text)) {
        continue;
      }
      const match = text.match(pricePattern);
      if (match?.[1]) {
        return cleanLine(match[1].replace(/\u00a0/g, " "));
      }
    }
    return null;
  };
  const structuralOffers = structuralOfferSignals();

  return {
    name: firstText(titleSelectors),
    secondary_name: firstText(["h2.bwoZTb span", "h2.bwoZTb"]),
    rating: firstText([
      "div.F7nice > span > span[aria-hidden='true']:first-child",
      "span.ceNzKf[role='img']",
      "span[role='img'][aria-label*='star']",
    ]),
    review_count: reviewCount,
    review_count_source: reviewSource,
    category: firstText([
      "button[jsaction*='category']",
      ".skqShb .fontBodyMedium button",
      "button.DkEaL",
    ]),
    price_range: priceRangeValue(),
    address: addressValue(),
    located_in: itemValue("locatedin"),
    status: firstText(["div.OqCZI .ZDu9vd", "div.OqCZI .o0Svhf"]),
    website: firstAttr(["a[data-item-id='authority']"], "href", document) || itemValue("authority"),
    reservation_links: collectReservationLinks(),
    phone: firstText([
      "button[data-item-id^='phone:'] .Io6YTe",
      "button[data-item-id^='phone:']",
    ]),
    plus_code: itemValue("oloc"),
    description: descriptionValue(),
    review_topics: collectReviewTopics(),
    admission_prices: collectLeafPrices(sectionRootByHeading([
      "Admission",
      "Ticket prices",
      "Entry fee",
      "Entrance fee",
      "入場",
      "入場料",
      "入園料",
      "票價",
      "票价",
      "門票",
      "门票",
    ])),
    room_prices: collectLeafPrices(sectionRootByHeading([
      "Compare prices",
      "Compare room prices",
      "Room prices",
      "價格比較",
      "价格比较",
      "比較價格",
      "比較房價",
      "料金を比較",
      "価格を比較",
      "宿泊料金を比較",
    ])),
    structural_offer_kind: structuralOffers.kind,
    structural_offer_prices: structuralOffers.prices,
    room_price_overlay: roomOverlayPrice(),
    dom_candidates: collectDomCandidates(),
    main_photo_url: mainPhotoUrl,
    photo_url: photoUrl,
    panel_text: panel?.innerText || "",
    body_text: document.body?.innerText || "",
    limited_view: (document.body?.innerText || "")
      .toLowerCase()
      .includes("limited view of google maps"),
  };
}
"""
_PLACE_REVIEW_SIGNAL_JS = r"""
() => {
""" + _PLACE_PANEL_HELPERS_JS + r"""

  const panel = placePanelRoot().root;
  const f7nice = panel.querySelector("div.F7nice");
  if (f7nice?.innerText?.match(/[0-9]/)) {
    return true;
  }
  for (const element of panel.querySelectorAll("[aria-label]")) {
    const label = element.getAttribute("aria-label") || "";
    if (/(review|reviews|評論|クチコミ)/i.test(label) && /[0-9]/.test(label)) {
      return true;
    }
  }
  for (const tab of panel.querySelectorAll("div[role='tablist'] button")) {
    if (/(review|reviews|評論|クチコミ)/i.test(tab.innerText || "")) {
      return true;
    }
  }
  return false;
}
"""
_PLACE_RESERVATION_BUTTON_CLICK_JS = r"""
() => {
""" + _PLACE_PANEL_HELPERS_JS + r"""

  const root = placePanelRoot().root;
  const reservationPattern = /\b(find a table|reserve|reservation|book a table)\b/i;
  const candidates = [
    ...root.querySelectorAll("button, div[role='button']"),
  ];
  for (const element of candidates) {
    const text = cleanLine(element.innerText || element.textContent || "");
    const ariaLabel = cleanLine(element.getAttribute("aria-label") || "");
    const title = cleanLine(element.getAttribute("title") || "");
    const itemId = cleanLine(element.getAttribute("data-item-id") || "");
    const evidence = `${text} ${ariaLabel} ${title} ${itemId}`;
    if (!reservationPattern.test(evidence)) {
      continue;
    }
    const {rect, visibleArea} = visibleRect(element);
    if (visibleArea <= 0 || rect.width <= 0 || rect.height <= 0) {
      continue;
    }
    element.click();
    return true;
  }
  return false;
}
"""
_PLACE_RESERVATION_DIALOG_JS = r"""
() => {
""" + _PLACE_PANEL_HELPERS_JS + r"""

  const providerLabelFromUrl = (href) => {
    try {
      const host = new URL(href).hostname.replace(/^www\./, "");
      const base = host.split(".")[0] || host;
      return base
        .replace(/[-_]+/g, " ")
        .replace(/\b\w/g, (char) => char.toUpperCase());
    } catch {
      return "Find a Table";
    }
  };
  const cleanReservationLabel = (value, href) => {
    const actionPrefixPattern = new RegExp(
      "^(?:find a table|reserve|make a reservation|book a table)"
        + "(?:\\s+(?:with|on|at|via))?\\s*",
      "i",
    );
    const cleaned = cleanLine(value)
      .replace(actionPrefixPattern, "")
      .replace(/\s+(?:opens in new tab|website)$/i, "")
      .trim();
    return cleaned || providerLabelFromUrl(href);
  };
  const providerHostPattern = new RegExp(
    "(opentable|resy|sevenrooms|thefork|tock|quandoo|yelp|inline|"
      + "tablecheck|exploretock|omakase|pocket-concierge|pocketconcierge|"
      + "tabelog|hotpepper|gnavi|gurunavi|ikyu|jpneazy|byfood|autoreserve)",
    "i",
  );
  const rejectHostPattern = new RegExp(
    String.raw`(^|\.)google(?:\.[a-z]{2,}){1,2}$`
      + String.raw`|(^|\.)gstatic\.com$`
      + String.raw`|(^|\.)googleusercontent\.com$`,
    "i",
  );
  const dialogs = [
    ...document.querySelectorAll("[role='dialog'], [aria-modal='true']"),
  ].filter((element) => {
    const {rect, visibleArea} = visibleRect(element);
    return visibleArea > 0 && rect.width >= 120 && rect.height >= 80;
  });
  const providerPanels = [
    ...document.querySelectorAll("div, section"),
  ].filter((element) => {
    const {rect, visibleArea} = visibleRect(element);
    if (visibleArea <= 0 || rect.width < 120 || rect.height < 80) {
      return false;
    }
    const text = cleanLine(element.innerText || element.textContent || "");
    return /\bcontinue with\b/i.test(text) && /\babout these providers\b/i.test(text);
  }).sort((left, right) => {
    const leftRect = left.getBoundingClientRect();
    const rightRect = right.getBoundingClientRect();
    return (leftRect.width * leftRect.height) - (rightRect.width * rightRect.height);
  });
  const providerRoots = dialogs.length ? dialogs : providerPanels.slice(0, 1);
  const roots = providerRoots.length ? providerRoots : [document.body];
  const hasTrustedProviderRoot = providerRoots.length > 0;
  const closeProviderRoot = (root) => {
    const closePattern = /(^|\b)(close|dismiss|cancel|modal-close|閉じ|關閉|关闭|닫기|닫|×)(\b|$)/i;
    const closeCandidates = "button, div[role='button'], [aria-label], [title]";
    for (const button of root.querySelectorAll(closeCandidates)) {
      const label = cleanLine([
        button.getAttribute("aria-label"),
        button.getAttribute("title"),
        button.innerText,
        button.textContent,
        button.className,
      ].filter(Boolean).join(" "));
      if (closePattern.test(label)) {
        button.click();
        return true;
      }
    }
    return false;
  };
  const links = [];
  const seen = new Set();
  for (const root of roots) {
    for (const element of root.querySelectorAll("a[href]")) {
      const href = element.href || element.getAttribute("href") || "";
      if (!/^https?:\/\//i.test(href) || seen.has(href)) {
        continue;
      }
      let host = "";
      try {
        host = new URL(href).hostname;
      } catch {
        continue;
      }
      const rawLabel = [
        element.innerText,
        element.textContent,
        element.getAttribute("aria-label"),
        element.getAttribute("title"),
      ].filter(Boolean).join(" ");
      const evidence = `${rawLabel} ${href}`;
      if (rejectHostPattern.test(host) && !/\/maps\/reserve\b/i.test(href)) {
        continue;
      }
      if (!hasTrustedProviderRoot && !providerHostPattern.test(evidence)) {
        continue;
      }
      seen.add(href);
      links.push({
        label: cleanReservationLabel(rawLabel, href),
        url: href,
      });
      if (links.length >= 8) {
        break;
      }
    }
    if (links.length >= 8) {
      break;
    }
  }
  for (const root of providerRoots) {
    if (closeProviderRoot(root)) {
      return links;
    }
  }
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  return links;
}
"""
_PLACE_REVIEW_TAB_CLICK_JS = r"""
() => {
""" + _PLACE_PANEL_HELPERS_JS + r"""

  const root = placePanelRoot().root;
  for (const tab of root.querySelectorAll("div[role='tablist'] button, button[role='tab']")) {
    const text = (tab.innerText || tab.textContent || "").trim();
    const ariaLabel = tab.getAttribute("aria-label") || "";
    if (/(review|reviews|評論|クチコミ)/i.test(`${text} ${ariaLabel}`)) {
      tab.click();
      return true;
    }
  }
  return false;
}
"""
_PLACE_REVIEW_TOPIC_JS = r"""
() => {
""" + _PLACE_PANEL_HELPERS_JS + r"""

  const root = placePanelRoot().root;
  for (const button of root.querySelectorAll("button, div[role='button']")) {
    const text = cleanLine(button.innerText || button.textContent || "");
    const ariaLabel = cleanLine(button.getAttribute("aria-label") || "");
    if (/^\+\d+$/.test(text) && !/photo/i.test(ariaLabel)) {
      button.click();
    }
  }
  const selectors = [
    "button[jsaction*='review']",
    "button[aria-label*='review' i]",
    "button[role='radio']",
    "button[aria-pressed]",
    "div[role='button'][aria-label]",
  ];
  const topics = [];
  const seen = new Set();
  for (const selector of selectors) {
    for (const element of root.querySelectorAll(selector)) {
      const text = cleanLine(element.innerText || element.textContent || "");
      const ariaLabel = cleanLine(element.getAttribute("aria-label") || "");
      const candidate = /[0-9]/.test(text)
        ? text
        : (/[0-9]/.test(ariaLabel) ? ariaLabel : text || ariaLabel);
      if (!candidate || candidate.length > 120 || !/[0-9]/.test(candidate)) {
        continue;
      }
      const key = `${candidate}\n${ariaLabel}`;
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      topics.push({
        text: candidate,
        aria_label: ariaLabel,
        source: selector,
      });
    }
  }
  return topics;
}
"""
_PLACE_REVIEW_SNIPPET_JS = r"""
() => {
  const cleanLine = (value) => (value || "").replace(/\s+/g, " ").trim();
  const reviewRoots = Array.from(document.querySelectorAll("[data-review-id], .jftiEf"));
  const reviews = [];
  const seen = new Set();
  for (const root of reviewRoots) {
    const text = cleanLine(
      root.querySelector(".MyEned .wiI7pd, .wiI7pd, .MyEned")?.innerText || ""
    );
    const author = cleanLine(
      root.querySelector(".d4r55, .WNxzHc, [aria-label^='Photo of']")?.innerText || ""
    );
    const ratingLabel = cleanLine(
      root.querySelector("[role='img'][aria-label*='star' i]")?.getAttribute("aria-label") || ""
    );
    const time = cleanLine(root.querySelector(".rsqaWe, .xRkPPb")?.innerText || "");
    const likeText = cleanLine(root.querySelector("button[jsaction*='like']")?.innerText || "");
    if (!text && !author) {
      continue;
    }
    const key = `${author}\n${time}\n${text}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    reviews.push({
      author,
      rating: ratingLabel,
      relative_time: time,
      text,
      like_count: likeText,
      source: "dom",
    });
    if (reviews.length >= 10) {
      break;
    }
  }
  return reviews;
}
"""
_PLACE_ABOUT_TAB_CLICK_JS = r"""
() => {
""" + _PLACE_PANEL_HELPERS_JS + r"""

  const aliases = [
    "about",
    "information",
    "details",
    "關於",
    "关于",
    "資訊",
    "信息",
    "詳細",
    "概要",
    "簡介",
    "简介",
  ];
  const root = placePanelRoot().root;
  for (const tab of root.querySelectorAll("div[role='tablist'] button, button[role='tab']")) {
    const text = ((tab.innerText || tab.textContent || "").trim()).toLowerCase();
    const ariaLabel = ((tab.getAttribute("aria-label") || "").trim()).toLowerCase();
    if (
      aliases.some((alias) => (
        text === alias || ariaLabel === alias || ariaLabel.endsWith(alias)
      ))
    ) {
      tab.click();
      return true;
    }
  }
  return false;
}
"""
_PLACE_SEARCH_RESULT_CLICK_JS = r"""
() => {
  const cleanLine = (value) => (value || "").replace(/\s+/g, " ").trim();
  const searchResultTitleLabels = new Set([
    "result",
    "results",
    "search result",
    "search results",
    "結果",
    "検索結果",
  ]);
  const isSearchResultTitle = (value) => searchResultTitleLabels.has(
    cleanLine(value).toLowerCase(),
  );
  const titleSelectors = ["h1.DUwDvf", "h1.lfPIob", "div[role='main'] h1"];
  for (const selector of titleSelectors) {
    const element = document.querySelector(selector);
    const title = cleanLine(element?.innerText || element?.textContent || "");
    if (title && !isSearchResultTitle(title)) {
      return false;
    }
  }

  const normalize = (value) => cleanLine(value)
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
  const queryUrl = new URL(window.location.href);
  const isSearchPage = /\/maps\/search(?:\/|$)/i.test(queryUrl.pathname)
    || queryUrl.searchParams.has("query")
    || queryUrl.searchParams.has("query_place_id");
  if (!isSearchPage) {
    return false;
  }
  const queryPlaceId = cleanLine(queryUrl.searchParams.get("query_place_id") || "");
  let queryText = cleanLine(queryUrl.searchParams.get("query") || "");
  if (!queryText) {
    const pathnameMatch = queryUrl.pathname.match(/\/maps\/search\/([^/]+)/i);
    if (pathnameMatch?.[1]) {
      try {
        queryText = decodeURIComponent(pathnameMatch[1]);
      } catch {
        queryText = pathnameMatch[1];
      }
    }
  }
  const normalizedQuery = normalize(queryText);
  const hasQueryContext = Boolean(queryPlaceId || normalizedQuery);
  const queryTokens = normalizedQuery.split(" ").filter((token) => token.length >= 3);
  const articles = Array.from(document.querySelectorAll("div[role='feed'] [role='article']"));
  let best = null;
  const cleanSearchLabel = (value) => cleanLine(value)
    .replace(/\s*·\s*(?:visited\s+)?link$/i, "")
    .trim();
  const textLines = (element) => {
    const text = element.innerText || element.textContent || "";
    return text.split(/\n+/).map(cleanLine).filter(Boolean);
  };
  const parseCardRating = (value) => {
    const text = cleanLine(value);
    const match = text.match(/([0-5](?:[.,][0-9]+)?)\s*(?:stars?|★)/i)
      || text.match(/^([0-5](?:[.,][0-9]+)?)$/);
    return match?.[1] || null;
  };
  const parseCardReviewCount = (value) => {
    const text = cleanLine(value);
    const match = text.match(
      /([0-9][0-9,.\s]*[KM萬万]?)\s*(?:reviews?|評論|クチコミ|件のクチコミ|件の Google クチコミ)/i,
    ) || text.match(/\(([0-9][0-9,.\s]*[KM萬万]?)\)/);
    return match?.[1] || null;
  };
  const parseCardCategoryAddress = (lines) => {
    for (const line of lines) {
      if (!line.includes("·")) {
        continue;
      }
      if (parseCardReviewCount(line) || parseCardRating(line)) {
        continue;
      }
      const segments = line.split("·").map(cleanLine).filter(Boolean);
      if (segments.length < 2) {
        continue;
      }
      return {
        category: segments[0] || null,
        address: segments[segments.length - 1] || null,
      };
    }
    return {category: null, address: null};
  };
  const cardDescription = (article, excludedValues) => {
    const excluded = new Set(excludedValues.map(cleanLine).filter(Boolean));
    const rows = Array.from(article.querySelectorAll("div.W4Efsd"));
    for (const row of rows) {
      const text = cleanLine(row.innerText || row.textContent || "");
      if (!text || excluded.has(text)) {
        continue;
      }
      if (text.includes("·") || parseCardRating(text) || parseCardReviewCount(text)) {
        continue;
      }
      if (
        row.querySelector(
          ".AJB7ye, .UsdlK, [role='img'][aria-label*='star' i], a[href^='tel:']",
        )
      ) {
        continue;
      }
      if (/^(open|closed|temporarily closed|website|directions|saved in)\b/i.test(text)) {
        continue;
      }
      if (/^[+()\d\s.-]{7,}$/.test(text)) {
        continue;
      }
      return text;
    }
    return null;
  };
  const safeDecodeURIComponent = (value) => {
    try {
      return decodeURIComponent(value);
    } catch {
      return value;
    }
  };
  const candidateDetails = (article, href, label) => {
    const lines = textLines(article);
    const starLabel = cleanLine(
      article.querySelector("[role='img'][aria-label*='star' i]")?.getAttribute("aria-label")
        || "",
    );
    const rating = parseCardRating(starLabel)
      || lines.map(parseCardRating).find(Boolean)
      || null;
    const reviewCount = parseCardReviewCount(starLabel)
      || lines.map(parseCardReviewCount).find(Boolean)
      || null;
    const categoryAddress = parseCardCategoryAddress(lines);
    const name = cleanSearchLabel(label);
    const placeIdMatch = href.match(/!19s([^!?&]+)/) || href.match(/!1s([^!?&]+)/);
    return {
      href,
      google_place_id: placeIdMatch?.[1] ? safeDecodeURIComponent(placeIdMatch[1]) : null,
      name,
      rating,
      review_count: reviewCount,
      category: categoryAddress.category,
      address: categoryAddress.address,
      search_result_description: cardDescription(
        article,
        [name, categoryAddress.category, categoryAddress.address],
      ),
      panel_text: lines.join("\n"),
      body_text: lines.join("\n"),
    };
  };

  for (let index = 0; index < articles.length; index += 1) {
    const article = articles[index];
    const anchor = article.querySelector("a.hfpxzc, a[aria-label], a[href*='/maps/place/']");
    if (!anchor) {
      continue;
    }
    const hrefValue = anchor.getAttribute("href") || anchor.href || "";
    let href = "";
    try {
      href = cleanLine(new URL(hrefValue, window.location.href).href);
    } catch {
      continue;
    }
    if (!href.includes("/maps/place/")) {
      continue;
    }
    const label = cleanLine(anchor.getAttribute("aria-label") || anchor.innerText || "");
    if (!label) {
      continue;
    }
    const normalizedLabel = normalize(label);
    const normalizedNearby = normalize(article.innerText || article.textContent || "");
    let score = hasQueryContext ? Math.max(0, 40 - index) : 0;

    if (queryPlaceId && href.includes(queryPlaceId)) {
      score += 200;
    }
    if (normalizedQuery) {
      if (normalizedLabel === normalizedQuery) {
        score += 120;
      } else {
        if (normalizedLabel && normalizedQuery.includes(normalizedLabel)) {
          score += 50;
        }
        if (normalizedQuery && normalizedLabel.includes(normalizedQuery)) {
          score += 40;
        }
      }
      if (normalizedNearby.includes(normalizedQuery)) {
        score += 30;
      }
    }
    for (const token of queryTokens) {
      if (normalizedLabel.includes(token)) {
        score += 10;
      }
      if (normalizedNearby.includes(token)) {
        score += 4;
      }
    }
    if (!best || score > best.score) {
      best = {score, details: candidateDetails(article, href, label)};
    }
  }

  if (!best || best.score <= 0) {
    return null;
  }
  return best.details || null;
}
"""
_PLACE_SEARCH_RESULT_OPEN_JS = r"""
(targetHref) => {
  const cleanLine = (value) => (value || "").replace(/\s+/g, " ").trim();
  let target = "";
  try {
    target = cleanLine(new URL(targetHref, window.location.href).href);
  } catch {
    return false;
  }
  if (!target) {
    return false;
  }
  const targetUrl = new URL(target);
  const targetPlaceIdMatch = target.match(/!19s([^!?&]+)/) || target.match(/!1s([^!?&]+)/);
  const targetPlaceId = targetPlaceIdMatch?.[1] || "";
  for (const anchor of document.querySelectorAll(
    "div[role='feed'] [role='article'] a.hfpxzc, "
      + "div[role='feed'] [role='article'] a[href*='/maps/place/']",
  )) {
    const hrefValue = anchor.getAttribute("href") || anchor.href || "";
    let href = "";
    try {
      href = cleanLine(new URL(hrefValue, window.location.href).href);
    } catch {
      continue;
    }
    const hrefPlaceIdMatch = href.match(/!19s([^!?&]+)/) || href.match(/!1s([^!?&]+)/);
    const hrefPlaceId = hrefPlaceIdMatch?.[1] || "";
    const hrefUrl = new URL(href);
    const samePlace = (
      href === target
      || (targetPlaceId && hrefPlaceId && targetPlaceId === hrefPlaceId)
      || (hrefUrl.pathname === targetUrl.pathname && hrefUrl.pathname.includes("/maps/place/"))
    );
    if (!samePlace) {
      continue;
    }
    const targetElement = anchor.closest("[role='article']") || anchor;
    targetElement.scrollIntoView({block: "center", inline: "center"});
    const rect = targetElement.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
      return false;
    }
    return {
      x: rect.left + Math.min(Math.max(rect.width / 2, 8), rect.width - 8),
      y: rect.top + Math.min(Math.max(rect.height / 2, 8), rect.height - 8),
    };
  }
  return false;
}
"""
_PLACE_DETAIL_READY_JS = r"""
() => {
""" + _PLACE_PANEL_HELPERS_JS + r"""

  const panelInfo = placePanelRoot();
  return panelInfo.found && Boolean(panelInfo.title) && !isSearchResultTitle(panelInfo.title);
}
"""
_PLACE_ABOUT_PANEL_JS = r"""
() => {
  const cleanLine = (value) => (value || "").replace(/\s+/g, " ").trim();
  const sections = [];
  const seenSections = new Set();
  const roots = Array.from(
    document.querySelectorAll("div[aria-label^='About '], div[role='region'][aria-label*='About']")
  );
  if (roots.length === 0) {
    roots.push(document.querySelector("div[role='main']") || document.body);
  }
  for (const root of roots) {
    for (const section of root.querySelectorAll(".iP2t7d, section")) {
      const title = cleanLine(section.querySelector("h2, h3")?.innerText || "");
      if (!title || seenSections.has(title)) {
        continue;
      }
      const items = [];
      const seenItems = new Set();
      for (const item of section.querySelectorAll("li span[aria-label]")) {
        if (item.closest("h1, h2, h3, button[role='tab']")) {
          continue;
        }
        const label = cleanLine(item.innerText || item.textContent || "");
        const ariaLabel = cleanLine(item.getAttribute("aria-label") || "");
        const candidate = label || ariaLabel;
        if (!candidate || candidate === title || candidate.length > 160) {
          continue;
        }
        const key = `${candidate}\n${ariaLabel}`;
        if (seenItems.has(key)) {
          continue;
        }
        seenItems.add(key);
        items.push({
          label: candidate,
          aria_label: ariaLabel,
          source: "about_panel",
        });
      }
      if (items.length > 0) {
        seenSections.add(title);
        sections.push({title, items});
      }
    }
  }
  return sections;
}
"""


def scrape_place(
    place_url: str,
    *,
    headless: bool = True,
    timeout_ms: int = 30_000,
    settle_time_ms: int = 3_000,
    browser_session: BrowserSessionConfig | None = None,
    http_session: HttpSessionConfig | None = None,
    llm_fallback: PlaceLLMRepairer | None = None,
    llm_policy: Literal["never", "on_quality_failure", "always"] = "on_quality_failure",
    llm_tasks: Sequence[PlaceLLMTask] = _DEFAULT_LLM_TASKS,
    collect_reviews: bool = True,
    collect_about: bool = True,
    screenshot_path: Path | None = None,
    overview_screenshot_path: Path | None = None,
) -> PlaceDetails:
    """Scrape a Google Maps place page using a browser session.

    If ``llm_fallback`` is provided, it receives a compact sanitized evidence
    packet and can return corrected Google Maps fields. The callback owns the
    provider, model, keys, and budget policy; this package only owns the
    generic Maps evidence schema.
    """
    snapshot = collect_place_snapshot(
        place_url,
        headless=headless,
        timeout_ms=timeout_ms,
        settle_time_ms=settle_time_ms,
        browser_session=browser_session,
        http_session=http_session,
        collect_reviews=collect_reviews,
        collect_about=collect_about,
        screenshot_path=screenshot_path,
        overview_screenshot_path=overview_screenshot_path,
    )
    return _build_place_details_from_snapshot(
        place_url,
        snapshot=snapshot,
        llm_fallback=llm_fallback,
        llm_policy=llm_policy,
        llm_tasks=llm_tasks,
    )


def scrape_places(
    place_urls: Sequence[str],
    *,
    headless: bool = True,
    timeout_ms: int = 30_000,
    settle_time_ms: int = 3_000,
    browser_session: BrowserSessionConfig | None = None,
    http_session: HttpSessionConfig | None = None,
    llm_fallback: PlaceLLMRepairer | None = None,
    llm_policy: Literal["never", "on_quality_failure", "always"] = "on_quality_failure",
    llm_tasks: Sequence[PlaceLLMTask] = _DEFAULT_LLM_TASKS,
    collect_reviews: bool = True,
    collect_about: bool = True,
    max_concurrency: int = 1,
    max_retries: int = 1,
    retry_backoff_ms: int = 2_000,
    stagger_ms: int = 0,
    retry_quality_flags: Sequence[str] = ("limited_view", "thin_place_result"),
    screenshot_output_dir: Path | None = None,
) -> list[PlaceScrapeResult]:
    """Scrape multiple Google Maps place pages.

    Sequential mode reuses one browser context across URLs. Parallel mode gives
    each worker its own browser context and, when a profile dir is configured,
    its own worker-scoped profile directory.
    """
    urls = [url.strip() for url in place_urls if url.strip()]
    if not urls:
        return []
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1.")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative.")
    if max_concurrency == 1:
        return _scrape_places_sequential(
            urls,
            headless=headless,
            timeout_ms=timeout_ms,
            settle_time_ms=settle_time_ms,
            browser_session=browser_session,
            http_session=http_session,
            llm_fallback=llm_fallback,
            llm_policy=llm_policy,
            llm_tasks=llm_tasks,
            collect_reviews=collect_reviews,
            collect_about=collect_about,
            max_retries=max_retries,
            retry_backoff_ms=retry_backoff_ms,
            stagger_ms=stagger_ms,
            retry_quality_flags=retry_quality_flags,
            screenshot_output_dir=screenshot_output_dir,
        )

    return _scrape_places_parallel(
        urls,
        headless=headless,
        timeout_ms=timeout_ms,
        settle_time_ms=settle_time_ms,
        browser_session=browser_session,
        http_session=http_session,
        llm_fallback=llm_fallback,
        llm_policy=llm_policy,
        llm_tasks=llm_tasks,
        collect_reviews=collect_reviews,
        collect_about=collect_about,
        max_concurrency=max_concurrency,
        max_retries=max_retries,
        retry_backoff_ms=retry_backoff_ms,
        stagger_ms=stagger_ms,
        retry_quality_flags=retry_quality_flags,
        screenshot_output_dir=screenshot_output_dir,
    )


def _build_place_details_from_snapshot(
    place_url: str,
    *,
    snapshot: Mapping[str, object],
    llm_fallback: PlaceLLMRepairer | None,
    llm_policy: Literal["never", "on_quality_failure", "always"],
    llm_tasks: Sequence[PlaceLLMTask] = _DEFAULT_LLM_TASKS,
) -> PlaceDetails:
    resolved_url = _normalize_response_url(snapshot.get("resolved_url"))
    if _looks_like_saved_list_url(resolved_url):
        raise ScrapeError(
            "Place URL resolved to a Google Maps saved list. "
            "Use `--kind list` for saved-list URLs or pass an individual place URL."
        )
    dom_snapshot = cast(Mapping[str, object], snapshot["dom"])
    preview_snapshot = cast(
        Mapping[str, object],
        snapshot.get("preview") if isinstance(snapshot.get("preview"), Mapping) else {},
    )
    search_result_snapshot = cast(
        Mapping[str, object],
        snapshot.get("search_result")
        if isinstance(snapshot.get("search_result"), Mapping)
        else {},
    )
    if search_result_snapshot and not _to_bool(
        search_result_snapshot.get("opened_from_search_result")
    ):
        # The page is still a search results list, so DOM may describe a
        # different visible card. Prefer richer preview data, then the selected
        # card, and only use search-page DOM as a last resort.
        merged_snapshot = _merge_ordered_place_sources(
            (preview_snapshot, "preview"),
            (search_result_snapshot, "search_result"),
            (dom_snapshot, "dom"),
        )
    else:
        merged_snapshot = _merge_ordered_place_sources(
            (dom_snapshot, "dom"),
            (preview_snapshot, "preview"),
            (search_result_snapshot, "search_result"),
        )
    details = _build_place_details(
        place_url,
        resolved_url=resolved_url,
        snapshot=merged_snapshot,
    )
    evidence = _build_place_llm_evidence(merged_snapshot)
    evidence_hash = _hash_evidence(evidence)
    details.diagnostics = _build_place_diagnostics(
        details,
        merged_snapshot,
        evidence_hash=evidence_hash,
    )
    normalized_tasks = _normalize_llm_tasks(llm_tasks)
    if llm_fallback is None or not _should_use_llm_repair(
        llm_policy,
        details.diagnostics,
        tasks=normalized_tasks,
    ):
        return details

    details.diagnostics.prompt_version = _PLACE_LLM_PROMPT_VERSION
    request_diagnostics = _diagnostics_for_llm_tasks(details.diagnostics, normalized_tasks)
    try:
        repair = llm_fallback(
            PlaceLLMRepairRequest(
                source_url=place_url,
                resolved_url=resolved_url,
                current_fields=_place_detail_values(details),
                diagnostics=request_diagnostics,
                evidence=evidence,
                tasks=list(normalized_tasks),
            )
        )
    except Exception as exc:
        details.diagnostics.llm_error = str(exc)
        details.diagnostics.prompt_version = _PLACE_LLM_PROMPT_VERSION
        return details
    if repair is None:
        return details

    repair_source = _extract_llm_repair_source(repair)
    repaired_snapshot = _merge_llm_place_fields(
        merged_snapshot,
        repair,
        current_fields=_place_detail_values(details),
        llm_tasks=normalized_tasks,
    )
    repaired_details = _build_place_details(
        place_url,
        resolved_url=resolved_url,
        snapshot=repaired_snapshot,
    )
    repaired_details.diagnostics = _build_place_diagnostics(
        repaired_details,
        repaired_snapshot,
        evidence_hash=evidence_hash,
        llm_used=_repair_source_used_llm(repair_source),
        repair_source=repair_source,
        prompt_version=_PLACE_LLM_PROMPT_VERSION,
    )
    return repaired_details


def _looks_like_saved_list_url(value: str | None) -> bool:
    return value is not None and extract_list_id(value) is not None


def _scrape_places_sequential(
    place_urls: Sequence[str],
    *,
    headless: bool,
    timeout_ms: int,
    settle_time_ms: int,
    browser_session: BrowserSessionConfig | None,
    http_session: HttpSessionConfig | None,
    llm_fallback: PlaceLLMRepairer | None,
    llm_policy: Literal["never", "on_quality_failure", "always"],
    llm_tasks: Sequence[PlaceLLMTask],
    collect_reviews: bool,
    collect_about: bool,
    max_retries: int,
    retry_backoff_ms: int,
    stagger_ms: int,
    retry_quality_flags: Sequence[str],
    screenshot_output_dir: Path | None,
) -> list[PlaceScrapeResult]:
    context = _launch_browser_context(
        headless=headless,
        browser_session=browser_session,
    )
    try:
        results: list[PlaceScrapeResult] = []
        for index, place_url in enumerate(place_urls):
            if index > 0 and stagger_ms > 0:
                time.sleep(stagger_ms / 1000)
            results.append(
                _scrape_place_with_context_and_retries(
                    place_url,
                    context=context,
                    timeout_ms=timeout_ms,
                    settle_time_ms=settle_time_ms,
                    http_session=http_session,
                    llm_fallback=llm_fallback,
                    llm_policy=llm_policy,
                    llm_tasks=llm_tasks,
                    collect_reviews=collect_reviews,
                    collect_about=collect_about,
                    max_retries=max_retries,
                    retry_backoff_ms=retry_backoff_ms,
                    retry_quality_flags=retry_quality_flags,
                    screenshot_path=_place_screenshot_path(
                        screenshot_output_dir,
                        place_url,
                        stage="reviews",
                    ),
                    overview_screenshot_path=_place_screenshot_path(
                        screenshot_output_dir,
                        place_url,
                        stage="overview",
                    ),
                )
            )
    finally:
        context.close()
    return results


def _scrape_places_parallel(
    place_urls: Sequence[str],
    *,
    headless: bool,
    timeout_ms: int,
    settle_time_ms: int,
    browser_session: BrowserSessionConfig | None,
    http_session: HttpSessionConfig | None,
    llm_fallback: PlaceLLMRepairer | None,
    llm_policy: Literal["never", "on_quality_failure", "always"],
    llm_tasks: Sequence[PlaceLLMTask],
    collect_reviews: bool,
    collect_about: bool,
    max_concurrency: int,
    max_retries: int,
    retry_backoff_ms: int,
    stagger_ms: int,
    retry_quality_flags: Sequence[str],
    screenshot_output_dir: Path | None,
) -> list[PlaceScrapeResult]:
    results: list[PlaceScrapeResult | None] = [None] * len(place_urls)
    worker_count = min(max_concurrency, len(place_urls))
    chunks: list[list[tuple[int, str]]] = [[] for _ in range(worker_count)]
    for index, place_url in enumerate(place_urls):
        chunks[index % worker_count].append((index, place_url))

    def scrape_worker(worker_index_and_items: tuple[int, list[tuple[int, str]]]) -> list[
        tuple[int, PlaceScrapeResult]
    ]:
        worker_index, items = worker_index_and_items
        if stagger_ms > 0:
            time.sleep(worker_index * stagger_ms / 1000)
        worker_session = _browser_session_for_parallel_worker(
            browser_session,
            worker_index=worker_index,
        )
        worker_http_session = _http_session_for_parallel_worker(
            http_session,
            worker_index=worker_index,
        )
        worker_urls = [place_url for _, place_url in items]
        worker_results = _scrape_places_sequential(
            worker_urls,
            headless=headless,
            timeout_ms=timeout_ms,
            settle_time_ms=settle_time_ms,
            browser_session=worker_session,
            http_session=worker_http_session,
            llm_fallback=llm_fallback,
            llm_policy=llm_policy,
            llm_tasks=llm_tasks,
            collect_reviews=collect_reviews,
            collect_about=collect_about,
            max_retries=max_retries,
            retry_backoff_ms=retry_backoff_ms,
            stagger_ms=stagger_ms,
            retry_quality_flags=retry_quality_flags,
            screenshot_output_dir=screenshot_output_dir,
        )
        return [
            (index, result)
            for (index, _place_url), result in zip(items, worker_results, strict=True)
        ]

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_items = {
            executor.submit(scrape_worker, (worker_index, chunk)): chunk
            for worker_index, chunk in enumerate(chunks)
            if chunk
        }
        for future in as_completed(future_items):
            try:
                worker_results = future.result()
            except Exception as exc:
                for index, place_url in future_items[future]:
                    results[index] = PlaceScrapeResult(
                        source_url=place_url,
                        attempts=0,
                        error=f"Parallel place worker failed: {exc}",
                    )
                continue
            for index, result in worker_results:
                results[index] = result

    return [result for result in results if result is not None]


def _browser_session_for_parallel_worker(
    browser_session: BrowserSessionConfig | None,
    *,
    worker_index: int,
) -> BrowserSessionConfig | None:
    if browser_session is None or browser_session.profile_dir is None:
        return browser_session
    return BrowserSessionConfig(
        profile_dir=browser_session.profile_dir / f"worker-{worker_index + 1}",
        proxy=browser_session.proxy,
    )


def _http_session_for_parallel_worker(
    http_session: HttpSessionConfig | None,
    *,
    worker_index: int,
) -> HttpSessionConfig | None:
    if http_session is None or http_session.cookie_jar_path is None:
        return http_session
    cookie_jar_path = http_session.cookie_jar_path
    return HttpSessionConfig(
        cookie_jar_path=(
            cookie_jar_path.parent
            / f"{cookie_jar_path.stem}.worker-{worker_index + 1}{cookie_jar_path.suffix}"
        ),
        proxy=http_session.proxy,
    )


def _scrape_place_with_context_and_retries(
    place_url: str,
    *,
    context: Any,
    timeout_ms: int,
    settle_time_ms: int,
    http_session: HttpSessionConfig | None,
    llm_fallback: PlaceLLMRepairer | None,
    llm_policy: Literal["never", "on_quality_failure", "always"],
    llm_tasks: Sequence[PlaceLLMTask],
    collect_reviews: bool,
    collect_about: bool,
    max_retries: int,
    retry_backoff_ms: int,
    retry_quality_flags: Sequence[str],
    screenshot_path: Path | None,
    overview_screenshot_path: Path | None,
) -> PlaceScrapeResult:
    attempts = 0
    last_error: str | None = None
    last_place: PlaceDetails | None = None
    while attempts <= max_retries:
        attempts += 1
        try:
            snapshot = _collect_place_snapshot_with_context(
                place_url,
                context=context,
                timeout_ms=timeout_ms,
                settle_time_ms=settle_time_ms,
                http_session=http_session,
                collect_reviews=collect_reviews,
                collect_about=collect_about,
                screenshot_path=screenshot_path,
                overview_screenshot_path=overview_screenshot_path,
            )
            place = _build_place_details_from_snapshot(
                place_url,
                snapshot=snapshot,
                llm_fallback=llm_fallback,
                llm_policy=llm_policy,
                llm_tasks=llm_tasks,
            )
        except Exception as exc:
            last_error = str(exc)
        else:
            last_place = place
            if not _should_retry_place_result(place, retry_quality_flags):
                return PlaceScrapeResult(
                    source_url=place_url,
                    place=place,
                    attempts=attempts,
                )
            last_error = "quality flags: " + ", ".join(
                place.diagnostics.quality_flags if place.diagnostics is not None else []
            )
        if attempts <= max_retries and retry_backoff_ms > 0:
            time.sleep((retry_backoff_ms * attempts) / 1000)
    return PlaceScrapeResult(
        source_url=place_url,
        place=last_place,
        error=last_error or "Place scrape failed.",
        attempts=attempts,
    )


def _should_retry_place_result(
    place: PlaceDetails,
    retry_quality_flags: Sequence[str],
) -> bool:
    if not retry_quality_flags or place.diagnostics is None:
        return False
    retry_flags = set(retry_quality_flags)
    return any(flag in retry_flags for flag in place.diagnostics.quality_flags)


def collect_place_snapshot(
    place_url: str,
    *,
    headless: bool = True,
    timeout_ms: int = 30_000,
    settle_time_ms: int = 3_000,
    browser_session: BrowserSessionConfig | None = None,
    http_session: HttpSessionConfig | None = None,
    collect_reviews: bool = True,
    collect_about: bool = True,
    screenshot_path: Path | None = None,
    overview_screenshot_path: Path | None = None,
) -> dict[str, object]:
    """Collect a normalized DOM snapshot for a Google Maps place page."""
    try:
        context = _launch_browser_context(
            headless=headless,
            browser_session=browser_session,
        )
    except Exception as exc:  # pragma: no cover - browser launch error path
        raise ScrapeError(f"Failed to launch browser context: {exc}") from exc
    try:
        return _collect_place_snapshot_with_context(
            place_url,
            context=context,
            timeout_ms=timeout_ms,
            settle_time_ms=settle_time_ms,
            http_session=http_session,
            collect_reviews=collect_reviews,
            collect_about=collect_about,
            screenshot_path=screenshot_path,
            overview_screenshot_path=overview_screenshot_path,
        )
    finally:
        context.close()


def _collect_place_snapshot_with_context(
    place_url: str,
    *,
    context: Any,
    timeout_ms: int,
    settle_time_ms: int,
    http_session: HttpSessionConfig | None,
    collect_reviews: bool,
    collect_about: bool,
    screenshot_path: Path | None,
    overview_screenshot_path: Path | None,
) -> dict[str, object]:
    page = None
    try:
        page = context.new_page()
        _seed_google_consent_cookies(page, source_url=place_url)
        page.goto(place_url, wait_until="domcontentloaded", timeout=timeout_ms)
        _handle_google_consent(page, timeout_ms=timeout_ms)
        try:
            page.wait_for_load_state("load", timeout=min(timeout_ms, 10_000))
        except Exception:
            pass
        _handle_google_consent(page, timeout_ms=timeout_ms)
        search_result_snapshot = _open_place_result_from_search_page(
            page,
            timeout_ms=timeout_ms,
        )
        try:
            page.wait_for_selector(_TITLE_SELECTOR, timeout=timeout_ms, state="attached")
        except Exception:
            pass
        if collect_reviews:
            _ensure_review_signal(page, timeout_ms=timeout_ms)
        page.wait_for_timeout(settle_time_ms)
        resolved_url = _normalize_response_url(getattr(page, "url", None))
        dom_snapshot = page.evaluate(_PLACE_JS_EXTRACTOR)
        if isinstance(dom_snapshot, Mapping):
            reservation_snapshot = _collect_reservation_dialog_snapshot(page, timeout_ms=timeout_ms)
            if reservation_snapshot:
                dom_snapshot = _merge_reservation_links(dom_snapshot, reservation_snapshot)
        if overview_screenshot_path is not None:
            _write_place_screenshot(page, overview_screenshot_path)
        if collect_reviews and isinstance(dom_snapshot, Mapping):
            review_snapshot = _collect_review_panel_snapshot(page, timeout_ms=timeout_ms)
            if review_snapshot:
                dom_snapshot = {**dom_snapshot, **review_snapshot}
        if screenshot_path is not None:
            _write_place_screenshot(page, screenshot_path)
        if collect_about and isinstance(dom_snapshot, Mapping):
            about_snapshot = _collect_about_panel_snapshot(page, timeout_ms=timeout_ms)
            if about_snapshot:
                dom_snapshot = {**dom_snapshot, **about_snapshot}
        preview_snapshot = _collect_preview_place_enrichment(
            place_url,
            resolved_url=resolved_url,
            timeout_ms=timeout_ms,
            http_session=http_session,
        )
    except Exception as exc:  # pragma: no cover - browser error path
        raise ScrapeError(f"Failed to scrape place page: {exc}") from exc
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    if not isinstance(dom_snapshot, Mapping):
        raise ScrapeError("Failed to collect a structured place snapshot from the page.")
    return {
        "resolved_url": resolved_url,
        "dom": dict(dom_snapshot),
        "search_result": search_result_snapshot or {},
        "preview": preview_snapshot,
    }


def _place_screenshot_path(
    output_dir: Path | None,
    place_url: str,
    *,
    stage: Literal["overview", "reviews"],
) -> Path | None:
    if output_dir is None:
        return None
    slug = "".join(character.lower() if character.isalnum() else "-" for character in place_url)
    slug = "-".join(part for part in slug.split("-") if part)
    digest = sha256(place_url.encode("utf-8")).hexdigest()[:8]
    return output_dir / f"{slug[:80] or 'place'}-{digest}-{stage}.png"


def _write_place_screenshot(page: Any, screenshot_path: Path) -> None:
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception:
        try:
            page.screenshot(path=str(screenshot_path))
        except Exception:
            return


def _wait_for_review_signal(page: Any, *, timeout_ms: int) -> bool:
    polls = max(1, min(6, timeout_ms // 1_000))
    for _ in range(polls):
        try:
            if page.evaluate(_PLACE_REVIEW_SIGNAL_JS) is True:
                return True
        except Exception:
            pass
        page.wait_for_timeout(500)
    return False


def _ensure_review_signal(page: Any, *, timeout_ms: int) -> bool:
    if _wait_for_review_signal(page, timeout_ms=timeout_ms):
        return True

    try:
        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception:
        return False

    _handle_google_consent(page, timeout_ms=timeout_ms)
    try:
        page.wait_for_load_state("load", timeout=min(timeout_ms, 10_000))
    except Exception:
        pass
    _handle_google_consent(page, timeout_ms=timeout_ms)
    try:
        page.wait_for_selector(_TITLE_SELECTOR, timeout=min(timeout_ms, 10_000), state="attached")
    except Exception:
        pass
    return _wait_for_review_signal(page, timeout_ms=min(timeout_ms, 4_000))


def _open_place_result_from_search_page(
    page: Any,
    *,
    timeout_ms: int,
) -> dict[str, object] | None:
    candidate = _search_result_candidate(page, timeout_ms=timeout_ms)
    if candidate is None:
        return None
    snapshot = _search_result_snapshot(candidate)
    target_url = (
        candidate
        if isinstance(candidate, str)
        else _clean_text(candidate.get("href"))
    )
    if target_url is None:
        return None
    if not _looks_like_google_maps_place_url(target_url):
        return None
    clicked = _click_search_result_candidate(page, target_url, timeout_ms=timeout_ms)
    if not clicked or not _wait_for_place_detail_title(page, timeout_ms=min(timeout_ms, 12_000)):
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            return snapshot
        _handle_google_consent(page, timeout_ms=timeout_ms)
        try:
            page.wait_for_load_state("load", timeout=min(timeout_ms, 10_000))
        except Exception:
            pass
        _handle_google_consent(page, timeout_ms=timeout_ms)
    if not _wait_for_place_detail_title(page, timeout_ms=min(timeout_ms, 12_000)):
        return snapshot
    snapshot["opened_from_search_result"] = True
    return snapshot


def _click_search_result_candidate(page: Any, target_url: str, *, timeout_ms: int) -> bool:
    try:
        click_point = page.evaluate(_PLACE_SEARCH_RESULT_OPEN_JS, target_url)
    except Exception:
        return False
    if not isinstance(click_point, Mapping):
        return False
    x = _parse_float(click_point.get("x"))
    y = _parse_float(click_point.get("y"))
    if x is None or y is None:
        return False
    mouse = getattr(page, "mouse", None)
    click = getattr(mouse, "click", None)
    if click is None:
        return False
    try:
        click(x, y)
    except Exception:
        return False
    page.wait_for_timeout(min(max(timeout_ms // 20, 1_000), 3_000))
    _handle_google_consent(page, timeout_ms=timeout_ms)
    try:
        page.wait_for_load_state("load", timeout=min(timeout_ms, 10_000))
    except Exception:
        pass
    _handle_google_consent(page, timeout_ms=timeout_ms)
    return True


def _wait_for_place_detail_title(page: Any, *, timeout_ms: int) -> bool:
    polls = max(1, min(24, timeout_ms // 500))
    for _ in range(polls):
        try:
            if page.evaluate(_PLACE_DETAIL_READY_JS) is True:
                return True
        except Exception:
            pass
        page.wait_for_timeout(500)
    return False


def _search_result_candidate_url(page: Any, *, timeout_ms: int) -> str | None:
    candidate = _search_result_candidate(page, timeout_ms=timeout_ms)
    if isinstance(candidate, str):
        return candidate
    if isinstance(candidate, Mapping):
        return _clean_text(candidate.get("href"))
    return None


def _search_result_candidate(
    page: Any,
    *,
    timeout_ms: int,
) -> str | dict[str, object] | None:
    polls = max(1, min(8, timeout_ms // 500))
    for _ in range(polls):
        try:
            candidate = page.evaluate(_PLACE_SEARCH_RESULT_CLICK_JS)
        except Exception:
            return None
        if candidate is False:
            return None
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, Mapping):
            href = _clean_text(candidate.get("href"))
            if href is not None:
                return {**candidate, "href": href}
        page.wait_for_timeout(500)
    return None


def _search_result_snapshot(candidate: str | Mapping[str, object]) -> dict[str, object]:
    if not isinstance(candidate, Mapping):
        return {}
    snapshot: dict[str, object] = {}
    href = _clean_text(candidate.get("href"))
    if href is not None:
        snapshot["search_result_url"] = href
    for key in (
        "google_place_id",
        "name",
        "rating",
        "review_count",
        "category",
        "address",
        "search_result_description",
        "panel_text",
        "body_text",
    ):
        value = candidate.get(key)
        if not _is_missing_value(value):
            snapshot[key] = value
    return snapshot


def _looks_like_google_maps_place_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if re.fullmatch(r"(?:www\.|maps\.)?google\.[a-z]{2,}(?:\.[a-z]{2,})?", host) is None:
        return False
    return parsed.path.startswith("/maps/place/")


def _collect_review_panel_snapshot(page: Any, *, timeout_ms: int) -> dict[str, object]:
    try:
        clicked = page.evaluate(_PLACE_REVIEW_TAB_CLICK_JS)
    except Exception:
        return {}
    if clicked is not True:
        return {}
    page.wait_for_timeout(min(max(timeout_ms // 10, 1_000), 3_000))
    try:
        topics = page.evaluate(_PLACE_REVIEW_TOPIC_JS)
        page.wait_for_timeout(500)
        expanded_topics = page.evaluate(_PLACE_REVIEW_TOPIC_JS)
        reviews = page.evaluate(_PLACE_REVIEW_SNIPPET_JS)
    except Exception:
        return {}
    result: dict[str, object] = {}
    if isinstance(expanded_topics, list) and len(expanded_topics) >= (
        len(topics) if isinstance(topics, list) else 0
    ):
        result["review_topics"] = expanded_topics
    elif isinstance(topics, list):
        result["review_topics"] = topics
    if isinstance(reviews, list):
        result["reviews"] = reviews
    return result


def _collect_reservation_dialog_snapshot(page: Any, *, timeout_ms: int) -> dict[str, object]:
    try:
        clicked = page.evaluate(_PLACE_RESERVATION_BUTTON_CLICK_JS)
    except Exception:
        return {}
    if clicked is not True:
        return {}
    page.wait_for_timeout(min(max(timeout_ms // 20, 500), 1_500))
    try:
        links = page.evaluate(_PLACE_RESERVATION_DIALOG_JS)
    except Exception:
        return {}
    if not isinstance(links, list):
        return {}
    return {"reservation_links": links}


def _merge_reservation_links(
    snapshot: Mapping[str, object],
    reservation_snapshot: Mapping[str, object],
) -> dict[str, object]:
    merged = dict(snapshot)
    links: list[object] = []
    seen_urls: set[str] = set()
    for source in (
        snapshot.get("reservation_links"),
        reservation_snapshot.get("reservation_links"),
    ):
        if not isinstance(source, list):
            continue
        for item in source:
            url = item.get("url") if isinstance(item, Mapping) else None
            if not isinstance(url, str) or url in seen_urls:
                continue
            seen_urls.add(url)
            links.append(item)
    if links:
        merged["reservation_links"] = links
    return merged


def _collect_about_panel_snapshot(page: Any, *, timeout_ms: int) -> dict[str, object]:
    try:
        clicked = page.evaluate(_PLACE_ABOUT_TAB_CLICK_JS)
    except Exception:
        return {}
    if clicked is not True:
        return {}
    page.wait_for_timeout(min(max(timeout_ms // 10, 1_000), 3_000))
    try:
        sections = page.evaluate(_PLACE_ABOUT_PANEL_JS)
    except Exception:
        return {}
    if not isinstance(sections, list):
        return {}
    return {"about_sections": sections}


def _build_place_details(
    source_url: str,
    *,
    resolved_url: str | None,
    snapshot: Mapping[str, object],
) -> PlaceDetails:
    panel_lines = _body_lines(snapshot.get("panel_text"))
    search_lines = panel_lines
    combined_lines = _dedupe_lines(panel_lines)
    category = _clean_category_text(snapshot.get("category")) or _extract_category_from_lines(
        search_lines
    )
    fallback_name = _first_meaningful_name(
        search_lines,
        excluded_values=(category,),
    )
    structured_name = _clean_structured_name_text(snapshot.get("name"))
    if (
        structured_name is not None
        and _looks_like_ui_action_label(structured_name)
        and _snapshot_contains_ui_action_name_candidate(snapshot)
    ):
        name = fallback_name
    else:
        name = structured_name or fallback_name
    category_display_en, category_display_en_source, category_display_en_confidence = (
        _derive_category_display_en(category, snapshot)
    )
    lat = _parse_float(snapshot.get("lat"))
    if lat is None:
        lat = _extract_coordinate_from_url(resolved_url or source_url, index=0)
    lng = _parse_float(snapshot.get("lng"))
    if lng is None:
        lng = _extract_coordinate_from_url(resolved_url or source_url, index=1)
    address = _clean_address_text(snapshot.get("address")) or _extract_address_from_lines(
        combined_lines
    )
    address_display_en, address_display_en_source, address_display_en_confidence = (
        _derive_address_display_en(address, snapshot)
    )
    structural_offer_kind = _clean_text(snapshot.get("structural_offer_kind"))
    structural_offer_prices = snapshot.get("structural_offer_prices")
    admission_prices = snapshot.get("admission_prices")
    room_prices = snapshot.get("room_prices")
    if structural_offer_kind == "admission" and not admission_prices:
        admission_prices = structural_offer_prices
    if structural_offer_kind == "room" and not room_prices:
        room_prices = structural_offer_prices
    price_range = _clean_price_range_text(
        snapshot.get("price_range")
    ) or _extract_price_range_from_lines(combined_lines)
    admission_price = _summarize_offer_prices(admission_prices) or (
        _extract_admission_price_from_lines(combined_lines)
    )
    admission_candidates = (
        {
            candidate
            for item in admission_prices
            if (candidate := _clean_numeric_price_text(item)) is not None
        }
        if isinstance(admission_prices, list)
        else set()
    )
    if (
        price_range is not None
        and re.fullmatch(r"\${1,4}", price_range) is None
        and (price_range == admission_price or price_range in admission_candidates)
    ):
        price_range = None
    return PlaceDetails(
        source_url=source_url,
        resolved_url=resolved_url,
        google_place_id=_normalize_google_place_id(snapshot.get("google_place_id")),
        name=name,
        secondary_name=_clean_name_text(snapshot.get("secondary_name"))
        or _extract_secondary_name(combined_lines, name=name),
        category=category,
        category_display_en=category_display_en,
        category_display_en_source=category_display_en_source,
        category_display_en_confidence=category_display_en_confidence,
        rating=_parse_rating(snapshot.get("rating")),
        review_count=_resolve_review_count(snapshot, combined_lines),
        price_range=price_range,
        admission_price=admission_price,
        room_price=_summarize_offer_prices(room_prices)
        or _clean_numeric_price_text(snapshot.get("room_price_overlay")),
        # Structural DOM data is primary. Text-line fallback is a last resort
        # for preview/limited payloads and is intentionally conservative.
        address=address,
        address_display_en=address_display_en,
        address_display_en_source=address_display_en_source,
        address_display_en_confidence=address_display_en_confidence,
        located_in=_clean_text(snapshot.get("located_in")),
        status=_clean_text(snapshot.get("status")) or _extract_status_from_lines(combined_lines),
        website=_normalize_website(snapshot.get("website")),
        reservation_links=_normalize_reservation_links(snapshot.get("reservation_links")),
        phone=_normalize_phone_candidate(snapshot.get("phone"))
        or _extract_phone_from_lines(combined_lines),
        plus_code=_clean_plus_code_text(snapshot.get("plus_code"))
        or _extract_plus_code_from_lines(combined_lines),
        address_parts=_extract_address_parts(snapshot.get("address_parts")),
        description=_extract_description(snapshot),
        search_result_description=_clean_description_text(
            snapshot.get("search_result_description")
        ),
        search_result_url=_clean_text(snapshot.get("search_result_url")),
        main_photo_url=_normalize_photo_url(snapshot.get("main_photo_url")),
        photo_url=_normalize_photo_url(snapshot.get("photo_url")),
        lat=lat,
        lng=lng,
        limited_view=_to_bool(snapshot.get("limited_view"))
        or any("limited view of google maps" in line.lower() for line in combined_lines),
        review_topics=_normalize_review_topics(snapshot.get("review_topics")),
        reviews=_normalize_reviews(snapshot.get("reviews")),
        about_sections=_normalize_about_sections(snapshot.get("about_sections")),
    )


def _seed_google_consent_cookies(page: Any, *, source_url: str) -> None:
    context = getattr(page, "context", None)
    add_cookies = getattr(context, "add_cookies", None)
    if not callable(add_cookies):
        return

    host = urlparse(source_url).hostname
    cookie_targets = ["https://www.google.com"]
    if isinstance(host, str) and host:
        cookie_targets.append(f"https://{host}")

    cookies = [
        {
            "name": "CONSENT",
            "value": "YES+cb.20240101-01-p0.en+FX+430",
            "url": target,
        }
        for target in cookie_targets
    ]
    try:
        add_cookies(cookies)
    except Exception:
        return


def _merge_place_sources(
    primary: Mapping[str, object],
    secondary: Mapping[str, object],
    *,
    secondary_source: str = "preview",
) -> dict[str, object]:
    merged = dict(primary)
    raw_field_sources = primary.get("field_sources")
    field_sources = dict(raw_field_sources) if isinstance(raw_field_sources, Mapping) else {}
    for key, value in primary.items():
        if key == "field_sources":
            continue
        if key not in field_sources and not _is_missing_value(value):
            field_sources[key] = "dom"
    for key, value in secondary.items():
        if key == "limited_view":
            merged[key] = _to_bool(merged.get(key)) or _to_bool(value)
            continue
        if _is_missing_value(merged.get(key)) and not _is_missing_value(value):
            merged[key] = value
            field_sources[key] = secondary_source
    merged["field_sources"] = field_sources
    return merged


def _merge_ordered_place_sources(
    *sources: tuple[Mapping[str, object], str],
) -> dict[str, object]:
    merged: dict[str, object] = {}
    for snapshot, source in sources:
        merged = _merge_place_sources(merged, snapshot, secondary_source=source)
    return merged


def _is_missing_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return len(value) == 0
    return False


_LLM_REPAIR_FIELDS = set(PLACE_LLM_REPAIR_FIELDS)
_QUALITY_CORE_FIELDS = ("name", "category", "rating", "review_count", "address")


def _normalize_llm_tasks(tasks: Sequence[PlaceLLMTask]) -> tuple[PlaceLLMTask, ...]:
    return tuple(dict.fromkeys(tasks))


def _llm_repair_fields_for_tasks(tasks: Sequence[PlaceLLMTask]) -> set[str]:
    if not tasks:
        return set()
    fields: set[str] = set()
    if "dom_repair" in tasks:
        fields.update(PLACE_LLM_DOM_REPAIR_FIELDS)
    if "display_translation" in tasks:
        fields.update(PLACE_LLM_DISPLAY_TRANSLATION_FIELDS)
    return fields


def _merge_llm_place_fields(
    snapshot: Mapping[str, object],
    repair: Mapping[str, object],
    *,
    current_fields: Mapping[str, object],
    llm_tasks: Sequence[PlaceLLMTask] = _DEFAULT_LLM_TASKS,
) -> dict[str, object]:
    raw_fields = repair.get("fields")
    fields = raw_fields if isinstance(raw_fields, Mapping) else repair
    merged = dict(snapshot)
    raw_sources = snapshot.get("field_sources")
    field_sources = dict(raw_sources) if isinstance(raw_sources, Mapping) else {}
    allowed_repair_fields = _llm_repair_fields_for_tasks(llm_tasks)

    for key, value in fields.items():
        if key == "_repair_source":
            continue
        if not isinstance(key, str) or key not in _LLM_REPAIR_FIELDS:
            continue
        if key not in allowed_repair_fields:
            continue
        if _is_missing_value(value):
            continue
        if not _is_missing_value(current_fields.get(key)):
            continue
        if key == "review_topics":
            value = _filter_llm_review_topics_by_evidence(value, snapshot.get("review_topics"))
            if _is_missing_value(value):
                continue
        if key == "about_sections":
            value = _filter_llm_about_sections_by_evidence(value, snapshot.get("about_sections"))
            if _is_missing_value(value):
                continue
        merged[key] = value
        field_sources[key] = "llm"

    merged["field_sources"] = field_sources
    return merged


def _extract_llm_repair_source(repair: Mapping[str, object]) -> str:
    raw_source = repair.get("_repair_source")
    if isinstance(raw_source, str) and raw_source.strip():
        return raw_source.strip()
    return "llm"


def _repair_source_used_llm(repair_source: str) -> bool:
    return repair_source not in {"cache", "translation_memory"}


def _filter_llm_review_topics_by_evidence(
    value: object,
    raw_evidence: object,
) -> list[dict[str, object]]:
    evidence_text = json.dumps(raw_evidence, ensure_ascii=False).casefold()
    if not evidence_text or evidence_text == "null":
        return []
    evidence_digits = re.sub(r"\D", "", evidence_text)
    if not isinstance(value, list):
        return []
    topics: list[ReviewTopic] = []
    labels_seen: dict[str, int] = {}
    for item in value:
        topic = _review_topic_from_mapping(item) if isinstance(item, Mapping) else None
        if topic is None:
            topic = _parse_review_topic_candidate(item)
        if topic is None:
            continue
        if topic.label.casefold() not in evidence_text:
            continue
        if topic.count is not None and str(topic.count) not in evidence_digits:
            continue
        key = topic.label.casefold()
        existing_index = labels_seen.get(key)
        if existing_index is None:
            labels_seen[key] = len(topics)
            topics.append(topic)
            continue
        existing = topics[existing_index]
        if existing.count is None or (
            topic.count is not None and topic.count > existing.count
        ):
            topics[existing_index] = topic
    return [topic.to_dict() for topic in topics]


def _filter_llm_about_sections_by_evidence(
    value: object,
    raw_evidence: object,
) -> list[dict[str, object]]:
    evidence_text = json.dumps(raw_evidence, ensure_ascii=False).casefold()
    if not evidence_text or evidence_text == "null":
        return []
    result: list[dict[str, object]] = []
    for section in _normalize_about_sections(value):
        kept_items = [
            item
            for item in section.items
            if item.label.casefold() in evidence_text
        ]
        if not kept_items:
            continue
        title = section.title
        if title.casefold() not in evidence_text:
            title = "About"
        result.append(
            PlaceAboutSection(title=title, items=kept_items).to_dict()
        )
    return result


def _derive_category_display_en(
    category: str | None,
    snapshot: Mapping[str, object],
) -> tuple[str | None, str | None, str | None]:
    direct = _clean_category_text(snapshot.get("category_display_en"))
    if direct is not None:
        source = _clean_text(snapshot.get("category_display_en_source")) or "llm"
        confidence = _clean_text(snapshot.get("category_display_en_confidence")) or "medium"
        return direct, source, confidence

    deterministic = _TRANSLATION_MEMORY.normalize_category(category)
    if deterministic is None:
        return None, None, None
    return deterministic.text, deterministic.source, deterministic.confidence


def _derive_address_display_en(
    address: str | None,
    snapshot: Mapping[str, object],
) -> tuple[str | None, str | None, str | None]:
    direct = _clean_address_display_en_text(snapshot.get("address_display_en"))
    if direct is not None:
        source = _clean_text(snapshot.get("address_display_en_source")) or "llm"
        confidence = _clean_text(snapshot.get("address_display_en_confidence")) or "medium"
        return direct, source, confidence

    deterministic = _TRANSLATION_MEMORY.normalize_address(address)
    if deterministic is None:
        return None, None, None
    return deterministic.text, deterministic.source, deterministic.confidence


def _clean_address_display_en_text(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None:
        return None
    lowered = normalized.lower()
    if _URL_LIKE_PATTERN.search(normalized) is not None:
        return None
    if any(fragment in lowered for fragment in _ADDRESS_REJECT_SUBSTRINGS):
        return None
    return normalized


def _needs_address_display_en(value: str | None) -> bool:
    return _needs_display_en(value)


def _needs_display_en(value: str | None) -> bool:
    return needs_display_en(value)


def _build_place_llm_evidence(snapshot: Mapping[str, object]) -> dict[str, object]:
    panel_lines = _body_lines(snapshot.get("panel_text"))
    body_lines = _body_lines(snapshot.get("body_text"))
    lines = _dedupe_lines([*panel_lines, *body_lines])
    return {
        "prompt_version": _PLACE_LLM_PROMPT_VERSION,
        "text_lines": lines[:80],
        "dom_candidates": _sanitize_dom_candidates(snapshot.get("dom_candidates"), limit=80),
        "review_topic_candidates": _sanitize_review_topic_candidates(
            snapshot.get("review_topics"),
            limit=50,
        ),
        "review_candidates": _sanitize_review_candidates(snapshot.get("reviews"), limit=10),
        "about_sections": _sanitize_about_sections(snapshot.get("about_sections"), limit=20),
    }


def _sanitize_dom_candidates(value: object, *, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    candidates: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        text = _clean_text(item.get("text")) or ""
        aria_label = _clean_text(item.get("aria_label")) or ""
        data_item_id = _clean_text(item.get("data_item_id")) or ""
        if not text and not aria_label and not data_item_id:
            continue
        key = (text, aria_label, data_item_id)
        if key in seen:
            continue
        seen.add(key)
        nearby = item.get("nearby_text")
        candidate: dict[str, object] = {
            "text": text[:240],
            "aria_label": aria_label[:240],
            "data_item_id": data_item_id[:120],
        }
        for attr in ("tag", "role", "selector_hint"):
            normalized = _clean_text(item.get(attr))
            if normalized is not None:
                candidate[attr] = normalized[:240]
        if isinstance(nearby, list):
            nearby_text: list[str] = []
            for value in nearby:
                normalized_nearby = _clean_text(value)
                if normalized_nearby is not None:
                    nearby_text.append(normalized_nearby[:240])
                if len(nearby_text) >= 4:
                    break
            if nearby_text:
                candidate["nearby_text"] = nearby_text
        candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


def _sanitize_review_topic_candidates(value: object, *, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    candidates: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            text = _clean_text(item.get("text") or item.get("label"))
            aria_label = _clean_text(item.get("aria_label"))
            source = _clean_text(item.get("source"))
        else:
            text = _clean_text(item)
            aria_label = None
            source = None
        if text is None and aria_label is None:
            continue
        candidate: dict[str, object] = {}
        if text is not None:
            candidate["text"] = text[:120]
        if aria_label is not None:
            candidate["aria_label"] = aria_label[:160]
        if source is not None:
            candidate["source"] = source[:80]
        candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


def _sanitize_review_candidates(value: object, *, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    candidates: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        candidate: dict[str, object] = {}
        for key in ("author", "rating", "relative_time", "text", "like_count", "source"):
            raw = item.get(key)
            if key in {"rating", "like_count"} and isinstance(raw, (int, float)):
                candidate[key] = raw
                continue
            normalized = _clean_text(raw)
            if normalized is not None:
                candidate[key] = normalized[:500] if key == "text" else normalized[:120]
        if candidate:
            candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


def _sanitize_about_sections(value: object, *, limit: int) -> list[dict[str, object]]:
    sections = _normalize_about_sections(value)
    return [section.to_dict() for section in sections[:limit]]


def _hash_evidence(evidence: Mapping[str, object]) -> str:
    payload = json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def _build_place_diagnostics(
    details: PlaceDetails,
    snapshot: Mapping[str, object],
    *,
    evidence_hash: str,
    llm_used: bool = False,
    repair_source: str | None = None,
    prompt_version: str | None = None,
) -> PlaceExtractionDiagnostics:
    values = _place_detail_values(details)
    missing_fields = [
        field for field in _QUALITY_CORE_FIELDS if _is_missing_value(values.get(field))
    ]
    quality_flags = [f"missing_{field}" for field in missing_fields]

    if details.limited_view:
        quality_flags.append("limited_view")
    if _needs_display_en(details.category) and details.category_display_en is None:
        quality_flags.append("needs_category_display_en")
    if _needs_address_display_en(details.address) and details.address_display_en is None:
        quality_flags.append("needs_address_display_en")

    page_url = f"{details.resolved_url or ''} {details.source_url}".lower()
    if "/maps/search" in page_url and (details.name is None or details.category is None):
        quality_flags.append("search_result_page")

    raw_address = _clean_text(snapshot.get("address"))
    if raw_address is not None and details.address is None:
        quality_flags.append("address_rejected")

    raw_name = _clean_text(snapshot.get("name"))
    if (
        details.name is None
        and (
            raw_name is not None
            and _looks_like_ui_action_label(raw_name)
            or _snapshot_contains_ui_action_name_candidate(snapshot)
        )
    ):
        quality_flags.append("name_rejected")

    if (
        details.rating is None
        and details.review_count is None
        and details.website is None
        and details.phone is None
    ):
        quality_flags.append("no_reputation_or_contact")

    if len(missing_fields) >= 3:
        quality_flags.append("thin_place_result")

    raw_sources = snapshot.get("field_sources")
    field_sources = {
        key: value
        for key, value in raw_sources.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and key in _LLM_REPAIR_FIELDS
        and not _is_missing_value(values.get(key))
    } if isinstance(raw_sources, Mapping) else {}
    if details.category_display_en is not None and details.category_display_en_source is not None:
        field_sources["category_display_en"] = details.category_display_en_source
    if details.address_display_en is not None and details.address_display_en_source is not None:
        field_sources["address_display_en"] = details.address_display_en_source
    confidence = _score_place_confidence(missing_fields, quality_flags)
    return PlaceExtractionDiagnostics(
        field_sources=field_sources,
        missing_fields=missing_fields,
        quality_flags=quality_flags,
        confidence=confidence,
        llm_used=llm_used,
        repair_source=repair_source,
        evidence_hash=evidence_hash,
        prompt_version=prompt_version,
    )


def _place_detail_values(details: PlaceDetails) -> dict[str, object]:
    return {
        "name": details.name,
        "secondary_name": details.secondary_name,
        "category": details.category,
        "category_display_en": details.category_display_en,
        "category_display_en_source": details.category_display_en_source,
        "category_display_en_confidence": details.category_display_en_confidence,
        "rating": details.rating,
        "review_count": details.review_count,
        "price_range": details.price_range,
        "admission_price": details.admission_price,
        "room_price": details.room_price,
        "address": details.address,
        "address_display_en": details.address_display_en,
        "address_display_en_source": details.address_display_en_source,
        "address_display_en_confidence": details.address_display_en_confidence,
        "located_in": details.located_in,
        "status": details.status,
        "website": details.website,
        "phone": details.phone,
        "plus_code": details.plus_code,
        "address_parts": details.address_parts,
        "description": details.description,
        "search_result_description": details.search_result_description,
        "search_result_url": details.search_result_url,
        "main_photo_url": details.main_photo_url,
        "photo_url": details.photo_url,
        "lat": details.lat,
        "lng": details.lng,
        "limited_view": details.limited_view,
        "google_place_id": details.google_place_id,
        "review_topics": [topic.to_dict() for topic in details.review_topics],
        "reviews": [review.to_dict() for review in details.reviews],
        "about_sections": [section.to_dict() for section in details.about_sections],
    }


def _score_place_confidence(missing_fields: list[str], quality_flags: list[str]) -> float:
    score = 1.0
    for field in missing_fields:
        score -= 0.2 if field == "name" else 0.12
    if "limited_view" in quality_flags:
        score -= 0.15
    if "search_result_page" in quality_flags:
        score -= 0.15
    if "address_rejected" in quality_flags:
        score -= 0.1
    if "thin_place_result" in quality_flags:
        score -= 0.15
    if "needs_category_display_en" in quality_flags:
        score -= 0.05
    return max(0.0, round(score, 2))


def _should_use_llm_repair(
    policy: Literal["never", "on_quality_failure", "always"],
    diagnostics: PlaceExtractionDiagnostics,
    *,
    tasks: Sequence[PlaceLLMTask] = _DEFAULT_LLM_TASKS,
) -> bool:
    normalized_tasks = _normalize_llm_tasks(tasks)
    if not normalized_tasks:
        return False
    if policy == "never":
        return False
    if policy == "always":
        return True
    if policy != "on_quality_failure":
        raise ValueError(f"Unsupported llm_policy: {policy}")
    quality_flags = set(diagnostics.quality_flags)
    if "dom_repair" in normalized_tasks and (
        quality_flags & _DOM_REPAIR_QUALITY_FLAGS
        or (diagnostics.confidence is not None and diagnostics.confidence < 0.7)
    ):
        return True
    return bool(
        "display_translation" in normalized_tasks
        and quality_flags & _DISPLAY_TRANSLATION_QUALITY_FLAGS
    )


def _diagnostics_for_llm_tasks(
    diagnostics: PlaceExtractionDiagnostics,
    tasks: Sequence[PlaceLLMTask],
) -> PlaceExtractionDiagnostics:
    allowed_flags = _llm_quality_flags_for_tasks(diagnostics.quality_flags, tasks)
    return PlaceExtractionDiagnostics(
        field_sources=dict(diagnostics.field_sources),
        missing_fields=list(diagnostics.missing_fields),
        quality_flags=allowed_flags,
        confidence=diagnostics.confidence,
        llm_used=diagnostics.llm_used,
        repair_source=diagnostics.repair_source,
        llm_error=diagnostics.llm_error,
        evidence_hash=diagnostics.evidence_hash,
        prompt_version=diagnostics.prompt_version,
    )


def _llm_quality_flags_for_tasks(
    quality_flags: Sequence[str],
    tasks: Sequence[PlaceLLMTask],
) -> list[str]:
    allowed: set[str] = set()
    if "dom_repair" in tasks:
        allowed.update(_DOM_REPAIR_QUALITY_FLAGS)
    if "display_translation" in tasks:
        allowed.update(_DISPLAY_TRANSLATION_QUALITY_FLAGS)
    return [flag for flag in quality_flags if flag in allowed]


def _collect_preview_place_enrichment(
    place_url: str,
    *,
    resolved_url: str | None,
    timeout_ms: int,
    http_session: HttpSessionConfig | None = None,
) -> dict[str, object]:
    curl_requests = _import_curl_requests()
    timeout_seconds = max(timeout_ms / 1_000, 1.0)
    base_url = resolved_url or place_url
    session_kwargs: dict[str, object] = {
        "impersonate": _HTTP_IMPERSONATE,
        "allow_redirects": True,
        "default_headers": True,
        "timeout": timeout_seconds,
    }
    cookie_jar = _load_http_cookie_jar(http_session)
    if cookie_jar is not None:
        session_kwargs["cookies"] = cookie_jar
    if http_session is not None and http_session.proxy is not None:
        session_kwargs["proxy"] = http_session.proxy

    try:
        with curl_requests.Session(**session_kwargs) as session:
            page_response = session.get(base_url)
            _raise_for_status(page_response)
            page_html = _response_text(page_response)
            preload_url = _extract_preloaded_fetch_url(
                page_html,
                base_url=base_url,
                preferred_path_markers=("preview/place",),
            )
            if preload_url is None:
                return {}
            preload_response = session.get(preload_url, referer=base_url)
            _raise_for_status(preload_response)
            payload_text = _response_text(preload_response)
    except Exception:
        return {}
    finally:
        _save_http_cookie_jar(http_session, cookie_jar)

    return _extract_preview_place_enrichment(payload_text)


def _extract_preview_place_enrichment(payload_text: str) -> dict[str, object]:
    root = _load_preview_payload(payload_text)
    if not isinstance(root, list):
        return {}

    strings = [value for value in _iter_strings(root) if _is_meaningful_preview_string(value)]
    enrichment: dict[str, object] = {}

    website = _extract_preview_website(strings)
    if website is not None:
        enrichment["website"] = website

    phone = _extract_preview_phone(strings)
    if phone is not None:
        enrichment["phone"] = phone

    plus_code = _extract_preview_plus_code(strings)
    if plus_code is not None:
        enrichment["plus_code"] = plus_code

    address_parts = _extract_preview_address_parts(root)
    if address_parts is not None:
        enrichment["address_parts"] = address_parts

    address = _extract_preview_address(strings)
    if address is not None:
        enrichment["address"] = address

    category = _extract_preview_category(root, strings)
    if category is not None:
        enrichment["category"] = category

    description = _extract_preview_description(strings)
    if description is not None:
        enrichment["description"] = description

    coordinates = _extract_preview_coordinates(root)
    if coordinates is not None:
        enrichment["lat"] = coordinates[0]
        enrichment["lng"] = coordinates[1]

    google_place_id = _extract_preview_google_place_id(root)
    if google_place_id is not None:
        enrichment["google_place_id"] = google_place_id

    return enrichment


def _load_preview_payload(payload_text: str) -> object:
    normalized = payload_text.strip()
    if normalized.startswith(")]}'"):
        normalized = normalized[4:].lstrip()
    try:
        return json.loads(normalized)
    except json.JSONDecodeError:
        return None


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return None
    return normalized


def _clean_address_text(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None:
        return None

    if "·" in normalized:
        segments = [segment.strip() for segment in normalized.split("·") if segment.strip()]
        for candidate in reversed(segments):
            if candidate == normalized:
                continue
            if _looks_like_address_line(candidate):
                return candidate

    lowered = normalized.lower()
    if _URL_LIKE_PATTERN.search(normalized) is not None:
        return None
    if any(fragment in lowered for fragment in _ADDRESS_REJECT_SUBSTRINGS):
        return None
    if any(fragment in lowered for fragment in _ADDRESS_REJECT_HOST_FRAGMENTS):
        return None
    if _TRAVEL_PRODUCT_ADDRESS_PATTERN.search(normalized) is not None:
        return None
    if _looks_like_review_snippet(normalized):
        return None
    if _ADDRESS_ENTITY_TOKEN_PATTERN.fullmatch(normalized):
        return None
    if any(keyword in lowered for keyword in _REVIEW_LABEL_KEYWORDS) and any(
        character.isdigit() for character in normalized
    ):
        return None
    if (
        normalized.endswith(".")
        and _PLUS_CODE_PATTERN.search(normalized) is None
        and not _looks_like_locality_address_line(normalized)
    ):
        return None

    if _looks_like_address_line(normalized):
        return normalized
    return None


def _clean_plus_code_text(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None:
        return None
    match = _PLUS_CODE_PATTERN.search(normalized)
    if match is None:
        return None
    return match.group(0).strip()


def _clean_name_text(value: object, *, reject_ui_actions: bool = True) -> str | None:
    normalized = _clean_text(value)
    if normalized is None:
        return None
    if _looks_like_status_text(normalized):
        return None
    if _looks_like_search_results_label(normalized):
        return None
    if reject_ui_actions and _looks_like_ui_action_label(normalized):
        return None
    if _looks_like_rating_text(normalized):
        return None
    if "·" in normalized and any(character.isdigit() for character in normalized):
        return None
    if any(character.isalnum() for character in normalized):
        return normalized
    return None


def _clean_structured_name_text(value: object) -> str | None:
    return _clean_name_text(value, reject_ui_actions=False)


def _clean_category_text(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None:
        return None
    if _looks_like_status_text(normalized):
        return None
    if _looks_like_search_results_label(normalized) or _looks_like_ui_action_label(normalized):
        return None
    if not any(character.isalpha() for character in normalized):
        return None
    return normalized


def _first_meaningful_name(
    lines: list[str],
    *,
    excluded_values: Sequence[str | None] = (),
) -> str | None:
    excluded = {
        value.casefold()
        for value in excluded_values
        if isinstance(value, str) and value.strip()
    }
    for line in lines:
        normalized = _clean_name_text(line)
        if normalized is not None and normalized.casefold() in excluded:
            continue
        if normalized is not None:
            return normalized
    return None


def _body_lines(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def _dedupe_lines(lines: Iterable[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if line in seen:
            continue
        deduped.append(line)
        seen.add(line)
    return deduped


def _extract_secondary_name(lines: list[str], *, name: str | None) -> str | None:
    if name is None:
        return None
    try:
        start = lines.index(name)
    except ValueError:
        return None
    for line in lines[start + 1 : start + 4]:
        if _parse_rating(line) is not None:
            return None
        normalized = _clean_name_text(line)
        if normalized is None or normalized == name:
            continue
        if _extract_category_from_lines([normalized]) is not None:
            return None
        return normalized
    return None


def _extract_category_from_lines(lines: list[str]) -> str | None:
    for line in lines:
        if "·" not in line:
            continue
        category = _clean_category_text(line.split("·", 1)[0].strip())
        if category:
            return category
    return None


def _extract_address_from_lines(lines: list[str]) -> str | None:
    for line in lines:
        if _looks_like_address_line(line):
            return line
    return None


def _looks_like_address_line(line: str) -> bool:
    lowered = line.lower()
    if _URL_LIKE_PATTERN.search(line) is not None:
        return False
    if any(fragment in lowered for fragment in _ADDRESS_REJECT_SUBSTRINGS):
        return False
    if any(fragment in lowered for fragment in _ADDRESS_REJECT_HOST_FRAGMENTS):
        return False
    if _TRAVEL_PRODUCT_ADDRESS_PATTERN.search(line) is not None:
        return False
    if "saved in" in lowered or "report a problem" in lowered:
        return False
    if _looks_like_review_snippet(line):
        return False
    if _looks_like_status_text(line):
        return False
    if _PHONE_PATTERN.match(line):
        return False
    if any(keyword in lowered for keyword in _REVIEW_LABEL_KEYWORDS) and any(
        character.isdigit() for character in line
    ):
        return False
    if _PLUS_CODE_PATTERN.search(line):
        return False
    if _parse_rating(line) is not None and "★" not in line and "star" not in lowered:
        if re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", line.strip()):
            return False
    if "〒" in line or line.startswith("Japan, "):
        return True
    if _POSTAL_CODE_PATTERN.search(line) and any(character.isalpha() for character in line):
        return True
    if re.search(r"\d", line) is None:
        return _looks_like_locality_address_line(line)
    if "," in line and any(character.isalpha() for character in line):
        if re.match(r"^\d+[A-Za-z0-9/-]*\b", line.strip()) and len(line.split()) <= 16:
            return True
        if _ADDRESS_KEYWORD_PATTERN.search(line) is None and len(line.split()) > 10:
            return False
        return True
    return _ADDRESS_KEYWORD_PATTERN.search(line) is not None


def _looks_like_locality_address_line(line: str) -> bool:
    """Return True for locality-only addresses when stronger markers are absent.

    Google sometimes exposes places such as notable streets as "Baku,
    Azerbaijan" with no street number or postal code. This is a fallback for
    those cases; structured address rows and digit/keyword addresses are handled
    before this function. Because Google body text also contains service chips
    and short review snippets, ambiguous text is rejected unless it looks like a
    compact locality chain.
    """
    if re.search(r"[!?]", line):
        return False
    if len(line.split()) > 8:
        return False
    parts = [part.strip() for part in line.split(",") if part.strip()]
    if not 2 <= len(parts) <= 4:
        return False
    if not all(_locality_part_allows_period(part) for part in parts):
        return False
    reject_keys = {
        key
        for part in parts
        if (key := _locality_address_reject_key(part)) in _LOCALITY_ADDRESS_REJECT_VALUES
    }
    # One segment can be a real place name ("Bar, Montenegro"). Two or more UI
    # labels are a strong signal this is a Google service/accessibility row. Use
    # unique matches so repeated locality names like "Bar, Bar, Montenegro"
    # still survive the fallback.
    if len(reject_keys) >= 2:
        return False
    return all(any(character.isalpha() for character in part) and len(part) <= 60 for part in parts)


def _locality_part_allows_period(part: str) -> bool:
    if "." not in part:
        return True
    if _LOCALITY_ABBREVIATION_PERIOD_PATTERN.fullmatch(part):
        return True
    return part.startswith("St. ") and len(part.split()) <= 3


def _locality_address_reject_key(part: str) -> str:
    return part.casefold().strip(" .")


def _has_address_marker(line: str) -> bool:
    return (
        _PLUS_CODE_PATTERN.search(line) is not None
        or _POSTAL_CODE_PATTERN.search(line) is not None
        or _STRONG_ADDRESS_KEYWORD_PATTERN.search(line) is not None
        or "〒" in line
        or line.startswith("Japan, ")
    )


def _looks_like_review_snippet(line: str) -> bool:
    if _has_address_marker(line):
        return False
    if line.endswith(" More"):
        return True
    terms = _PROSE_TERM_PATTERN.findall(line)
    word_count = len(line.split())
    # Short comma-separated review fragments can otherwise look like locality
    # pairs. Require each segment to be phrase-like so names such as "Friendly,
    # Coffee Springs" are not rejected only because words overlap review prose.
    if "," in line and len(terms) >= 2 and all(len(part.split()) >= 2 for part in line.split(",")):
        return True
    if word_count >= 10 and len(terms) >= 2:
        return True
    if word_count >= 10 and re.search(r"[.!?]", line) and terms:
        return True
    return False


def _extract_status_from_lines(lines: list[str]) -> str | None:
    for line in lines:
        if _looks_like_status_text(line):
            return line
    return None


def _extract_phone_from_lines(lines: list[str]) -> str | None:
    for line in lines:
        normalized = _normalize_phone_candidate(line)
        if normalized is not None:
            return normalized
    return None


def _clean_price_range_text(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None or len(normalized) > 80:
        return None
    normalized = normalized.replace("\u00a0", " ")
    match = _PRICE_RANGE_PATTERN.search(normalized)
    if match is None:
        return None
    return _clean_text(match.group(0))


def _extract_price_range_from_lines(lines: list[str]) -> str | None:
    for line in lines:
        if not _looks_like_price_range_line(line):
            continue
        normalized = _clean_price_range_text(line)
        if normalized is not None:
            return normalized
    return None


def _extract_admission_price_from_lines(lines: list[str]) -> str | None:
    candidates: list[str] = []
    capture_following_prices = 0
    for line in lines:
        normalized = _clean_text(line)
        if normalized is None:
            continue
        if _ADMISSION_CONTEXT_PATTERN.search(normalized):
            capture_following_prices = 2
            price = _clean_numeric_price_text(normalized)
            if price is not None:
                candidates.append(price)
            continue
        if capture_following_prices > 0:
            capture_following_prices -= 1
            price = _clean_numeric_price_text(normalized)
            if price is not None:
                candidates.append(price)
    if not candidates:
        return None
    return _summarize_offer_prices(candidates)


def _looks_like_price_range_line(value: object) -> bool:
    normalized = _clean_text(value)
    if normalized is None:
        return False
    if re.fullmatch(r"\${1,4}", normalized):
        return True
    if "·" not in normalized:
        return False
    if _PRICE_RANGE_PATTERN.search(normalized) is None:
        return False
    if _CATEGORY_SUFFIX_PATTERN.search(normalized) is not None:
        return True
    if re.search(r"(?:reviews?|評論|クチコミ)", normalized, re.IGNORECASE):
        return True
    return (
        re.search(r"^[0-5](?:[.,][0-9])?\s*(?:[·⋅]|★|stars?\b)", normalized, re.IGNORECASE)
        is not None
        and re.search(r"\([0-9][0-9,.\s]*\)", normalized) is not None
    )


def _clean_numeric_price_text(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None or len(normalized) > 80:
        return None
    normalized = normalized.replace("\u00a0", " ")
    match = _NUMERIC_PRICE_PATTERN.search(normalized)
    if match is None:
        return None
    return _clean_text(match.group(0))


def _summarize_offer_prices(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    parsed_candidates: list[tuple[str, float, str]] = []
    for raw_candidate in value:
        price_text = _clean_numeric_price_text(raw_candidate)
        if price_text is None:
            continue
        parsed = _parse_numeric_price(price_text)
        if parsed is None:
            continue
        parsed_candidates.append(parsed)
    if not parsed_candidates:
        return None

    currency_counts: dict[str, int] = {}
    for currency, _amount, _display in parsed_candidates:
        currency_counts[currency] = currency_counts.get(currency, 0) + 1
    primary_currency = max(currency_counts.items(), key=lambda item: item[1])[0]
    currency_candidates = [
        candidate for candidate in parsed_candidates if candidate[0] == primary_currency
    ]
    median_amount = statistics.median(amount for _currency, amount, _display in currency_candidates)
    best_currency, best_amount, best_display = min(
        currency_candidates,
        key=lambda candidate: (abs(candidate[1] - median_amount), candidate[1]),
    )
    del best_currency, best_amount
    return best_display


def _parse_numeric_price(value: str) -> tuple[str, float, str] | None:
    match = _NUMERIC_PRICE_PATTERN.search(value)
    if match is None:
        return None
    currency = match.group("currency").upper().replace(" ", "")
    amount = _parse_price_amount(match.group("amount"))
    if amount is None:
        return None
    return (currency, amount, value)


def _parse_price_amount(value: str) -> float | None:
    normalized = value.replace("\u00a0", "").replace(" ", "")
    if not normalized:
        return None
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif normalized.count(",") == 1 and "." not in normalized:
        whole, fractional = normalized.split(",", 1)
        if len(fractional) in {1, 2}:
            normalized = f"{whole}.{fractional}"
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized and "." not in normalized:
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", normalized):
            normalized = normalized.replace(",", "")
        else:
            return None
    elif "." in normalized and "," not in normalized:
        if re.fullmatch(r"\d{1,3}(?:[.]\d{3})+", normalized):
            normalized = normalized.replace(".", "")
    else:
        normalized = normalized.replace(",", "")
    try:
        return float(normalized)
    except ValueError:
        return None


def _extract_plus_code_from_lines(lines: list[str]) -> str | None:
    for line in lines:
        match = _PLUS_CODE_PATTERN.search(line)
        if match is not None:
            return match.group(0).strip()
    return None


def _extract_description(snapshot: Mapping[str, object]) -> str | None:
    return _clean_description_text(snapshot.get("description"))


def _clean_description_text(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None:
        return None
    normalized = _strip_description_service_options(normalized)
    if normalized is None:
        return None
    lowered = normalized.lower()
    if lowered in _DESCRIPTION_STOP_MARKERS or lowered.strip(" .") in _DESCRIPTION_STOP_MARKER_KEYS:
        return None
    if any(marker in lowered for marker in _DESCRIPTION_STOP_SUBSTRINGS):
        return None
    if _looks_like_status_text(normalized):
        return None
    if (
        _looks_like_search_results_label(normalized)
        or _looks_like_ui_action_label(normalized)
        or _looks_like_ui_action_cluster(normalized)
    ):
        return None
    if (
        _looks_like_description_review_prose(normalized)
        or _looks_like_review_response_text(normalized)
        or _looks_like_first_person_review_text(normalized)
    ):
        return None
    if not any(character.isalnum() for character in normalized):
        return None
    if _normalize_phone_candidate(normalized) is not None:
        return None
    if _looks_like_address_line(normalized):
        return None
    if (
        _parse_rating(normalized) is not None
        and not any(character.isalpha() for character in normalized)
    ):
        return None
    return normalized


def _looks_like_ui_action_cluster(value: str) -> bool:
    text = re.sub(r"[\ue000-\uf8ff]", " ", value)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return _UI_ACTION_CLUSTER_RE.fullmatch(text) is not None


def _strip_description_service_options(value: str) -> str | None:
    segments = [_clean_description_segment(part) for part in re.split(r"[·•⋅]+", value)]
    cleaned_segments = [segment for segment in segments if segment]
    if not cleaned_segments:
        return None

    is_service_segment = [
        bool(_DESCRIPTION_SERVICE_OPTION_SEGMENT_PATTERN.fullmatch(segment))
        for segment in cleaned_segments
    ]
    if all(is_service_segment):
        return None

    first_non_service_index = 0
    while (
        first_non_service_index < len(cleaned_segments)
        and is_service_segment[first_non_service_index]
    ):
        first_non_service_index += 1
    last_non_service_index = len(cleaned_segments) - 1
    while (
        last_non_service_index >= first_non_service_index
        and is_service_segment[last_non_service_index]
    ):
        last_non_service_index -= 1

    kept_segments = cleaned_segments[first_non_service_index : last_non_service_index + 1]
    if len(kept_segments) == 1 and _looks_like_category_text(kept_segments[0]):
        return None
    trimmed = " · ".join(kept_segments).strip()
    if first_non_service_index > 0 or last_non_service_index < len(cleaned_segments) - 1:
        trimmed = trimmed.strip(" .")
    return trimmed or None


def _clean_description_segment(value: str) -> str | None:
    normalized = _clean_text(value)
    if normalized is None:
        return None
    normalized = normalized.strip("·•⋅ ").strip()
    normalized = re.sub(r"^[✓✔☑✗✕✖\ue5ca\ue5cb\ue5cc\ue5cd\ue5cf]+\s*", "", normalized)
    normalized = re.sub(r"\s*[✓✔☑✗✕✖\ue5ca\ue5cb\ue5cc\ue5cd\ue5cf]+$", "", normalized)
    normalized = normalized.strip()
    return normalized or None


def _looks_like_review_response_text(value: str) -> bool:
    lowered = value.casefold().replace("’", "'")
    if len(value.split()) < 10:
        return False
    return any(marker in lowered for marker in _DESCRIPTION_REVIEW_RESPONSE_MARKERS)


def _looks_like_description_review_prose(value: str) -> bool:
    if len(value.split()) < 12:
        return False
    lowered = value.casefold()
    return any(marker in lowered for marker in _DESCRIPTION_REVIEW_PROSE_MARKERS)


def _looks_like_first_person_review_text(value: str) -> bool:
    if len(value.split()) < 12:
        return False
    if _DESCRIPTION_FIRST_PERSON_PRONOUN_PATTERN.search(value) is None:
        return False
    return _DESCRIPTION_FIRST_PERSON_EXPERIENCE_PATTERN.search(value) is not None


def _extract_preview_website(strings: list[str]) -> str | None:
    for value in strings:
        for candidate in re.findall(r"https?://[^\s\"'<>]+", value):
            normalized = _normalize_preview_website(candidate)
            if normalized is not None:
                return normalized
    return None


def _normalize_preview_website(value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.endswith("google.com") or parsed.netloc.endswith("gstatic.com"):
        query = parse_qs(parsed.query)
        target = query.get("q", [None])[0]
        if target is None:
            return None
        return _normalize_preview_website(unquote(target))
    if "googleusercontent.com" in parsed.netloc:
        return None
    if "streetviewpixels-pa.googleapis.com" in parsed.netloc:
        return None
    if parsed.netloc.endswith("inline.app"):
        return None
    return value


def _normalize_photo_url(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if host.endswith("gstatic.com") and (
        "result-no-thumbnail" in path
        or "default_geocode" in path
        or "mapslogo" in path
    ):
        return None
    if path.startswith("/maps/api/staticmap") and (
        host in {"maps.google.com", "www.google.com", "maps.googleapis.com"}
        or host.endswith(".googleapis.com")
    ):
        return None
    if "streetviewpixels-pa.googleapis.com" in host:
        return None
    if (
        "googleusercontent.com" in host or host.endswith("ggpht.com")
    ) and path.startswith(("/a-", "/a/")):
        return None
    if re.fullmatch(r"lh[0-9]+\.(?:googleusercontent\.com|ggpht\.com)", host) is None:
        return None
    if re.search(r"(?:=|-)w[0-9]+-h[0-9]+(?:-|$)", normalized) is None:
        return None
    return normalized


def _normalize_google_place_id(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None:
        return None
    return normalized if _GOOGLE_PLACE_ID_PATTERN.fullmatch(normalized) else None


_GOOGLE_PLACE_ID_PATTERN = re.compile(r"ChIJ[0-9A-Za-z_-]{10,}")
_MAPS_ENTITY_TOKEN_PATTERN = re.compile(r"0x[0-9a-fA-F]+:0x[0-9a-fA-F]+")
_KNOWLEDGE_GRAPH_MID_PATTERN = re.compile(r"^/m/[A-Za-z0-9_-]+$")


def _extract_preview_google_place_id(root: list[object]) -> str | None:
    unique_place_ids: list[str] = []
    seen_place_ids: set[str] = set()

    for node in _iter_lists(root):
        strings = [value for value in node if isinstance(value, str)]
        if not strings:
            continue

        place_ids = [value for value in strings if _GOOGLE_PLACE_ID_PATTERN.fullmatch(value)]
        if not place_ids:
            continue

        for place_id in place_ids:
            if place_id not in seen_place_ids:
                unique_place_ids.append(place_id)
                seen_place_ids.add(place_id)

        if any(_MAPS_ENTITY_TOKEN_PATTERN.fullmatch(value) for value in strings) or any(
            _KNOWLEDGE_GRAPH_MID_PATTERN.fullmatch(value) for value in strings
        ):
            return place_ids[0]

    if len(unique_place_ids) == 1:
        return unique_place_ids[0]
    return None


def _extract_address_parts(value: object) -> AddressParts | None:
    if not isinstance(value, list):
        return None
    return _normalize_address_parts(value)


def _normalize_address_parts(value: list[object]) -> AddressParts | None:
    if len(value) < 7 or len(value) > 8:
        return None
    if not all(isinstance(item, str) for item in value[:7]):
        return None
    normalized: AddressParts = [cast(str, item) for item in value[:7]]
    if len(value) == 8:
        extra = value[7]
        if not isinstance(extra, list) or not all(isinstance(item, str) for item in extra):
            return None
        normalized.append([cast(str, item) for item in extra])
    return normalized


def _normalize_review_topics(value: object) -> list[ReviewTopic]:
    if not isinstance(value, list):
        return []
    topics: list[ReviewTopic] = []
    labels_seen: dict[str, int] = {}
    for item in value:
        topic = _review_topic_from_mapping(item) if isinstance(item, Mapping) else None
        if topic is None:
            topic = _parse_review_topic_candidate(item)
        if topic is None:
            continue
        key = topic.label.casefold()
        existing_index = labels_seen.get(key)
        if existing_index is None:
            labels_seen[key] = len(topics)
            topics.append(topic)
            continue
        existing = topics[existing_index]
        if existing.count is None or (
            topic.count is not None and topic.count > existing.count
        ):
            topics[existing_index] = topic
    return topics


def _normalize_about_sections(value: object) -> list[PlaceAboutSection]:
    if not isinstance(value, list):
        return []
    sections: list[PlaceAboutSection] = []
    seen_titles: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        title = _clean_text(item.get("title"))
        if title is None or title.casefold() in seen_titles:
            continue
        raw_items = item.get("items")
        if not isinstance(raw_items, list):
            continue
        about_items: list[PlaceAboutItem] = []
        seen_labels: set[str] = set()
        for raw_item in raw_items:
            about_item = _normalize_about_item(raw_item)
            if about_item is None:
                continue
            key = about_item.label.casefold()
            if key in seen_labels:
                continue
            seen_labels.add(key)
            about_items.append(about_item)
        if not about_items:
            continue
        seen_titles.add(title.casefold())
        sections.append(PlaceAboutSection(title=title, items=about_items))
    return sections


def _normalize_about_item(value: object) -> PlaceAboutItem | None:
    if isinstance(value, Mapping):
        label = _clean_about_label(value.get("label"))
        aria_label = _clean_text(value.get("aria_label"))
        source = _clean_text(value.get("source"))
    else:
        label = _clean_about_label(value)
        aria_label = None
        source = None
    if label is None:
        return None
    return PlaceAboutItem(label=label, aria_label=aria_label, source=source)


def _clean_about_label(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None or len(normalized) > 160:
        return None
    if _URL_LIKE_PATTERN.search(normalized) is not None:
        return None
    if normalized in {"✓", ""}:
        return None
    normalized = normalized.removeprefix("✓ ").strip()
    normalized = re.sub(r"^[\ue000-\uf8ff]\s*", "", normalized).strip()
    return normalized or None


def _review_topic_from_mapping(value: Mapping[str, object]) -> ReviewTopic | None:
    direct_label = _clean_review_topic_label(value.get("label"))
    direct_count = _parse_review_count(value.get("count"))
    source = _clean_text(value.get("source"))
    if direct_label is not None:
        return ReviewTopic(label=direct_label, count=direct_count, source=source)

    text = _clean_text(value.get("text"))
    aria_label = _clean_text(value.get("aria_label"))
    for candidate in (aria_label, text):
        topic = _parse_review_topic_candidate(candidate)
        if topic is not None:
            return ReviewTopic(label=topic.label, count=topic.count, source=source)
    return None


def _parse_review_topic_candidate(value: object) -> ReviewTopic | None:
    normalized = _clean_text(value)
    if normalized is None or len(normalized) > 160:
        return None
    patterns = (
        r"^(?P<label>.+?),?\s+mentioned\s+in\s+(?P<count>[0-9][0-9,.\s]*[KM萬万]?)\s+"
        r"(?:reviews?|評論|クチコミ)\b$",
        r"^(?:mentioned\s+in\s+)?(?P<count>[0-9][0-9,.\s]*[KM萬万]?)\s+"
        r"(?:reviews?|評論|クチコミ)\b[^:：]*[:：]\s*(?P<label>.+)$",
        r"^(?P<label>.+?)\s*[(](?P<count>[0-9][0-9,.\s]*[KM萬万]?)[)]$",
        r"^(?P<label>.+?)\s+(?P<count>[0-9][0-9,.\s]*[KM萬万]?)$",
        r"^(?P<count>[0-9][0-9,.\s]*[KM萬万]?)\s+(?P<label>.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, normalized, flags=re.IGNORECASE)
        if match is None:
            continue
        label = _clean_review_topic_label(match.group("label"))
        count = _parse_review_count(match.group("count"))
        if label is not None and count is not None:
            return ReviewTopic(label=label, count=count)
    return None


def _clean_review_topic_label(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None:
        return None
    normalized = normalized.strip(" \"'“”‘’()[]")
    normalized = re.sub(
        r"\s+[0-9][0-9,.\s]*[KM萬万]?$",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip()
    if not normalized or len(normalized) > 50:
        return None
    lowered = normalized.casefold()
    if lowered in _REVIEW_TOPIC_REJECT_LABELS:
        return None
    if any(term in lowered for term in _REVIEW_TOPIC_REJECT_TERMS):
        return None
    if "rating" in lowered:
        return None
    if re.fullmatch(r"(?:[1-5]\s+)?stars?", lowered):
        return None
    if any(keyword in lowered for keyword in _REVIEW_LABEL_KEYWORDS):
        return None
    if _URL_LIKE_PATTERN.search(normalized) is not None:
        return None
    if not any(character.isalpha() for character in normalized):
        return None
    if _parse_rating(normalized) is not None and re.fullmatch(
        r"[0-9]+(?:[.,][0-9]+)?",
        normalized,
    ):
        return None
    return normalized


def _normalize_reviews(value: object) -> list[PlaceReview]:
    if not isinstance(value, list):
        return []
    raw_reviews: list[PlaceReview] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        text = _clean_review_text(item.get("text"))
        author = _clean_text(item.get("author"))
        relative_time = _clean_text(item.get("relative_time"))
        if text is None and author is None:
            continue
        key = (author, relative_time, text)
        if key in seen:
            continue
        seen.add(key)
        raw_reviews.append(
            PlaceReview(
                author=author,
                rating=_parse_rating(item.get("rating")),
                relative_time=relative_time,
                text=text,
                like_count=_parse_review_like_count(item.get("like_count")),
                source=_clean_text(item.get("source")),
            )
        )
    return _merge_adjacent_review_fragments(raw_reviews)


def _merge_adjacent_review_fragments(reviews: list[PlaceReview]) -> list[PlaceReview]:
    merged: list[PlaceReview] = []
    pending_author: PlaceReview | None = None
    for review in reviews:
        if review.text is None and review.author is not None:
            if merged and merged[-1].author is None and merged[-1].text is not None:
                previous = merged[-1]
                merged[-1] = PlaceReview(
                    author=review.author,
                    rating=previous.rating,
                    relative_time=previous.relative_time,
                    text=previous.text,
                    like_count=previous.like_count,
                    source=previous.source or review.source,
                )
            else:
                pending_author = review
            continue
        if pending_author is not None and review.author is None:
            review = PlaceReview(
                author=pending_author.author,
                rating=review.rating,
                relative_time=review.relative_time,
                text=review.text,
                like_count=review.like_count,
                source=review.source or pending_author.source,
            )
            pending_author = None
        merged.append(review)
    return merged


def _clean_review_text(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None:
        return None
    return normalized.removesuffix(" More").strip()


def _parse_review_like_count(value: object) -> int | None:
    if isinstance(value, int):
        return value
    normalized = _clean_text(value)
    if normalized is None or normalized.casefold() == "like":
        return None
    match = re.search(r"\b([0-9][0-9,.\s]*)\b", normalized)
    if match is None:
        return None
    return _parse_review_count(match.group(1))


def _extract_preview_phone(strings: list[str]) -> str | None:
    best_local: str | None = None
    for value in strings:
        normalized = _normalize_phone_candidate(value)
        if normalized is None:
            continue
        if normalized.startswith("+"):
            return normalized
        if best_local is None:
            best_local = normalized
    return best_local


def _extract_preview_plus_code(strings: list[str]) -> str | None:
    compound_match: str | None = None
    for value in strings:
        match = _PLUS_CODE_PATTERN.search(value)
        if match is not None:
            candidate = match.group(0).strip()
            if " " in candidate:
                return candidate
            if compound_match is None:
                compound_match = candidate
    return compound_match


def _extract_preview_address_parts(root: list[object]) -> AddressParts | None:
    for node in _iter_lists(root):
        if len(node) < 2:
            continue
        raw_parts = node[0]
        raw_plus_code = node[1]
        if not isinstance(raw_parts, list) or not isinstance(raw_plus_code, list):
            continue
        normalized_parts = _normalize_address_parts(raw_parts)
        if normalized_parts is None:
            continue
        if not any(
            isinstance(value, list)
            and any(
                isinstance(item, str) and _PLUS_CODE_PATTERN.search(item) is not None
                for item in value
            )
            for value in raw_plus_code
        ):
            continue
        return normalized_parts
    return None


def _extract_preview_address(strings: list[str]) -> str | None:
    candidates: list[str] = []
    for value in strings:
        normalized = _clean_text(value)
        if normalized is None:
            continue
        if "maps/preview/place" in normalized or normalized.startswith("/g/"):
            continue
        cleaned = _clean_address_text(normalized)
        if cleaned is not None and _looks_like_address_line(cleaned):
            candidates.append(cleaned)
    if not candidates:
        return None
    return max(candidates, key=len)


def _extract_preview_category(root: list[object], strings: list[str]) -> str | None:
    for node in _iter_lists(root):
        if not node or not all(isinstance(value, str) for value in node):
            continue
        text_items = [cast(str, value).strip() for value in node]
        if (
            1 <= len(text_items) <= 4
            and all(_looks_like_category_text(item) for item in text_items)
        ):
            return _clean_category_text(text_items[0])

    for value in strings:
        if not value.startswith("SearchResult.TYPE_"):
            continue
        category = value.removeprefix("SearchResult.TYPE_").replace("_", " ").strip().lower()
        if category:
            return _clean_category_text(category.capitalize())
    return None


def _looks_like_category_text(value: str) -> bool:
    if not value or len(value) > 60:
        return False
    if re.search(r"\d", value):
        return False
    if value.startswith(("http://", "https://", "/g/")):
        return False
    if "," in value:
        return False
    return _CATEGORY_SUFFIX_PATTERN.search(value) is not None


def _extract_preview_description(strings: list[str]) -> str | None:
    candidates = [
        value.strip()
        for value in strings
        if len(value.split()) >= 4
        and "SearchResult.TYPE_" not in value
        and "support.google.com" not in value
        and "local/content/rap/report" not in value
        and "〒" not in value
        and not value.startswith("Japan, ")
        and value.count(",") < 2
        and not _looks_like_status_text(value)
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


def _normalize_phone_candidate(value: object) -> str | None:
    normalized = _clean_text(value)
    if normalized is None or not _PHONE_PATTERN.match(normalized):
        return None
    digit_count = sum(character.isdigit() for character in normalized)
    if digit_count < 8 or digit_count > 15:
        return None
    if normalized.isdigit() and digit_count == 13 and normalized.startswith("17"):
        return None
    return normalized


def _looks_like_status_text(value: str) -> bool:
    normalized = _clean_text(value)
    if normalized is None:
        return False
    if _STATUS_LINE_PATTERN.match(normalized):
        return True
    return any(marker in normalized for marker in ("営業時間", "営業開始", "営業終了"))


def _looks_like_search_results_label(value: str) -> bool:
    normalized = _clean_text(value)
    if normalized is None:
        return False
    normalized_lookup = normalized.casefold()
    return normalized_lookup in _SEARCH_RESULTS_LABELS or normalized_lookup.startswith(
        "sponsored "
    )


def _looks_like_ui_action_label(value: str) -> bool:
    normalized = _clean_text(value)
    if normalized is None:
        return False
    return normalized.casefold() in _UI_ACTION_LABELS


def _snapshot_contains_ui_action_name_candidate(snapshot: Mapping[str, object]) -> bool:
    lines = _dedupe_lines(
        [
            *_body_lines(snapshot.get("panel_text")),
            *_body_lines(snapshot.get("body_text")),
        ]
    )
    return any(_looks_like_ui_action_label(line) for line in lines)


def _extract_preview_coordinates(root: list[object]) -> tuple[float, float] | None:
    fallback_e7_pair: tuple[float, float] | None = None
    for node in _iter_lists(root):
        if len(node) == 4 and node[0] is None and node[1] is None:
            lat = _parse_float(node[2])
            lng = _parse_float(node[3])
            if _valid_coordinates(lat, lng):
                return (cast(float, lat), cast(float, lng))
        if len(node) == 2 and all(isinstance(value, int) for value in node):
            lat_e7 = cast(int, node[0])
            lng_e7 = cast(int, node[1])
            if not _looks_like_e7_coordinate_pair(lat_e7, lng_e7):
                continue
            lat = lat_e7 / 10_000_000
            lng = lng_e7 / 10_000_000
            if _valid_coordinates(lat, lng) and fallback_e7_pair is None:
                fallback_e7_pair = (lat, lng)
    return fallback_e7_pair


def _looks_like_e7_coordinate_pair(lat_e7: int, lng_e7: int) -> bool:
    if lat_e7 == 0 and lng_e7 == 0:
        return True
    return max(abs(lat_e7), abs(lng_e7)) >= 10_000


def _iter_strings(node: object) -> Iterable[str]:
    if isinstance(node, str):
        yield node
        return
    if isinstance(node, list):
        for item in node:
            yield from _iter_strings(item)
        return
    if isinstance(node, dict):
        for item in node.values():
            yield from _iter_strings(item)


def _iter_lists(node: object) -> Iterable[list[object]]:
    if isinstance(node, list):
        yield node
        for item in node:
            yield from _iter_lists(item)
        return
    if isinstance(node, dict):
        for item in node.values():
            yield from _iter_lists(item)


def _is_meaningful_preview_string(value: str) -> bool:
    normalized = value.strip()
    if not normalized or len(normalized) > 400:
        return False
    if (
        normalized.startswith("0ahUKE")
        or normalized.startswith("EvgD")
        or normalized.startswith("UF3g")
    ):
        return False
    return True


def _parse_rating(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = re.search(r"([0-9]+(?:[.,][0-9]+)?)", value)
    if match is None:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _looks_like_rating_text(value: str) -> bool:
    stripped = value.strip()
    if re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", stripped):
        rating = _parse_rating(stripped)
        return rating is not None and 0 <= rating <= 5
    return re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?\s*\([0-9,]+\)", stripped) is not None


def _parse_review_count(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str):
        return None
    match = re.search(r"([0-9][0-9,.\s]*)([KM萬万]?)", value.strip(), re.IGNORECASE)
    if match is None:
        return None
    number_text = match.group(1).strip()
    suffix = match.group(2).upper()
    if not suffix and re.fullmatch(r"\d{1,3}(?:[.,\s]\d{3})+", number_text):
        return int(re.sub(r"[.,\s]", "", number_text))
    try:
        number = float(number_text.replace(",", "").replace(" ", ""))
    except ValueError:
        return None
    multiplier = 1
    if suffix == "K":
        multiplier = 1_000
    elif suffix == "M":
        multiplier = 1_000_000
    elif suffix in {"萬", "万"}:
        multiplier = 10_000
    return int(number * multiplier)


def _resolve_review_count(snapshot: Mapping[str, object], lines: list[str]) -> int | None:
    direct_count = _parse_review_count(snapshot.get("review_count"))
    if direct_count is not None:
        return direct_count
    from_lines = _extract_review_count_from_lines(lines)
    if from_lines is not None:
        return from_lines
    return None


def _extract_review_count_from_lines(lines: list[str]) -> int | None:
    for line in lines:
        match = re.fullmatch(
            r"\(?([0-9][0-9,.\s]*[KM萬万]?)\)?\s+"
            r"(?:reviews?|評論|クチコミ|件のクチコミ|件の Google クチコミ)",
            line.strip(),
            flags=re.IGNORECASE,
        )
        if match is None:
            continue
        return _parse_review_count(match.group(1))

    for index, line in enumerate(lines[:-1]):
        if _parse_rating(line) is None:
            continue
        match = re.match(r"^\(?([0-9][0-9,.\s]*[KM萬万]?)\)?(?:\s*[·⋅].*)?$", lines[index + 1])
        if match is None:
            continue
        count = _parse_review_count(match.group(1))
        if count is not None and count >= 10:
            return count
    return None


def _normalize_website(value: object) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    return _normalize_preview_website(text)


def _normalize_reservation_links(value: object) -> list[PlaceReservationLink]:
    if not isinstance(value, list):
        return []
    links: list[PlaceReservationLink] = []
    seen_urls: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        raw_label = item.get("label")
        raw_url = item.get("url")
        url = _clean_text(raw_url)
        if url is None:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        normalized_url = _normalize_preview_website(url) if "google.com" in parsed.netloc else url
        if normalized_url is None:
            normalized_url = url if parsed.netloc.endswith("google.com") else None
        if normalized_url is None or normalized_url in seen_urls:
            continue
        label = _clean_reservation_label(raw_label, normalized_url)
        links.append(PlaceReservationLink(label=label[:80], url=normalized_url))
        seen_urls.add(normalized_url)
    if any(not _reservation_link_is_google_reserve(link.url) for link in links):
        links = [link for link in links if not _reservation_link_is_google_reserve(link.url)]
    return links


def _reservation_link_is_google_reserve(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return (
        host in {"www.google.com", "google.com", "maps.google.com"}
        and parsed.path.startswith("/maps/reserve")
    )


def _clean_reservation_label(value: object, url: str) -> str:
    fallback = _reservation_provider_label_from_url(url)
    label = _clean_text(value)
    if label is None:
        return fallback
    label = re.sub(r"[\ue000-\uf8ff]", " ", label)
    label = re.sub(
        r"^(?:find a table|reserve|make a reservation|book a table)"
        r"(?:\s+(?:with|on|at|via))?\s*",
        "",
        label,
        flags=re.IGNORECASE,
    )
    label = re.sub(r"\s+(?:opens in new tab|website)$", "", label, flags=re.IGNORECASE)
    label = _clean_text(label)
    if label is None:
        return fallback

    tokens = label.split()
    unique_tokens = list(dict.fromkeys(token.casefold() for token in tokens))
    if len(unique_tokens) == 1 and len(tokens) > 1:
        label = tokens[0]
    if "." in label or re.fullmatch(r"(?:https?://)?[A-Za-z0-9.-]+/?", label):
        return fallback
    return label


def _reservation_provider_label_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    known_hosts = (
        ("tablecheck.", "TableCheck"),
        ("resy.", "Resy"),
        ("opentable.", "OpenTable"),
        ("sevenrooms.", "SevenRooms"),
        ("thefork.", "TheFork"),
        ("exploretock.", "Tock"),
        ("tock.", "Tock"),
        ("quandoo.", "Quandoo"),
        ("omakase.", "Omakase"),
        ("pocket-concierge.", "Pocket Concierge"),
        ("pocketconcierge.", "Pocket Concierge"),
        ("tabelog.", "Tabelog"),
        ("hotpepper.", "Hot Pepper"),
        ("gnavi.", "Gurunavi"),
        ("gurunavi.", "Gurunavi"),
        ("ikyu.", "Ikyu"),
        ("jpneazy.", "JPNEAZY"),
        ("byfood.", "ByFood"),
        ("autoreserve.", "AutoReserve"),
        ("sg-management.", "SG Management"),
    )
    for marker, label in known_hosts:
        if marker in host:
            return label
    base = host.split(".")[0] if host else ""
    if not base:
        return "Find a Table"
    return base.replace("-", " ").replace("_", " ").title()


def _extract_coordinate_from_url(url: str, *, index: int) -> float | None:
    match = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", url)
    if match is None:
        return None
    try:
        return float(match.group(index + 1))
    except ValueError:
        return None


def _to_bool(value: object) -> bool:
    return value is True


def _parse_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _valid_coordinates(lat: float | None, lng: float | None) -> bool:
    if lat is None or lng is None:
        return False
    return -90 <= lat <= 90 and -180 <= lng <= 180
