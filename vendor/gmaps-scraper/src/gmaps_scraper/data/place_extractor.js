
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
      ].filter(Boolean).join(" ");
      if (!reservationPattern.test(evidence)) {
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
