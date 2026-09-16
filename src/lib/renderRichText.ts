function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatPlainText(value: string): string {
  return escapeHtml(value).replace(/\r\n?/g, "\n").replace(/\n/g, "<br />");
}

function hasMoreClosersThanOpeners(value: string, openChar: string, closeChar: string): boolean {
  let opens = 0;
  let closes = 0;

  for (const char of value) {
    if (char === openChar) {
      opens += 1;
    } else if (char === closeChar) {
      closes += 1;
    }
  }

  return closes > opens;
}

function splitTrailingUrlPunctuation(value: string): [string, string] {
  let url = value;
  let trailing = "";

  while (url.length > 0) {
    const lastChar = url.at(-1);
    if (!lastChar) {
      break;
    }

    if (/[.,!?;:]/.test(lastChar)) {
      trailing = `${lastChar}${trailing}`;
      url = url.slice(0, -1);
      continue;
    }

    if (lastChar === ")" && hasMoreClosersThanOpeners(url, "(", ")")) {
      trailing = `${lastChar}${trailing}`;
      url = url.slice(0, -1);
      continue;
    }

    if (lastChar === "]" && hasMoreClosersThanOpeners(url, "[", "]")) {
      trailing = `${lastChar}${trailing}`;
      url = url.slice(0, -1);
      continue;
    }

    if (lastChar === "}" && hasMoreClosersThanOpeners(url, "{", "}")) {
      trailing = `${lastChar}${trailing}`;
      url = url.slice(0, -1);
      continue;
    }

    break;
  }

  return [url, trailing];
}

function normalizeUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return null;
    }
    return parsed.toString();
  } catch {
    return null;
  }
}

function renderAnchor(href: string, label: string): string {
  return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
}

const RICH_TEXT_PATTERN = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<]+)/gi;

export function renderRichText(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }

  let html = "";
  let lastIndex = 0;

  for (const match of value.matchAll(RICH_TEXT_PATTERN)) {
    const index = match.index ?? 0;
    html += formatPlainText(value.slice(lastIndex, index));

    const [fullMatch, markdownLabel, markdownUrl, rawUrl] = match;

    if (markdownLabel && markdownUrl) {
      const href = normalizeUrl(markdownUrl);
      html += href ? renderAnchor(href, escapeHtml(markdownLabel)) : formatPlainText(fullMatch);
    } else if (rawUrl) {
      const [trimmedUrl, trailingPunctuation] = splitTrailingUrlPunctuation(rawUrl);
      const href = normalizeUrl(trimmedUrl);

      if (href) {
        html += renderAnchor(href, escapeHtml(trimmedUrl));
        html += escapeHtml(trailingPunctuation);
      } else {
        html += formatPlainText(fullMatch);
      }
    }

    lastIndex = index + fullMatch.length;
  }

  html += formatPlainText(value.slice(lastIndex));
  return html;
}
