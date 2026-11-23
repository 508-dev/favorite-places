import { APPLY_FILTERS_ACTION, createFilterAction, filterActionLabels } from "./chat-actions.js";

const root = document.querySelector("[data-place-chat]");

if (root) {
  const toggle = root.querySelector("[data-place-chat-toggle]");
  const close = root.querySelector("[data-place-chat-close]");
  const panel = root.querySelector("[data-place-chat-panel]");
  const thread = root.querySelector("[data-place-chat-thread]");
  const form = root.querySelector("[data-place-chat-form]");
  const input = root.querySelector("[data-place-chat-input]");
  const submit = root.querySelector("[data-place-chat-submit]");
  const status = root.querySelector("[data-place-chat-status]");
  const messages = [];
  let searchContext;
  let quotaResetAt = 0;
  document.querySelector("[data-guide-root]")?.addEventListener("guide:filters-result", (event) => {
    status.textContent = event.detail?.applied
      ? `Showing ${event.detail.count} matching places. Undo is available above the results.`
      : "Those filters match no saved places. Try a broader request.";
    if (event.detail?.applied && window.matchMedia("(max-width: 640px)").matches) setOpen(false);
  });

  const setOpen = (open) => {
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) input.focus({ preventScroll: true });
    else toggle.focus({ preventScroll: true });
  };

  toggle.addEventListener("click", () => setOpen(panel.hidden));
  close.addEventListener("click", () => setOpen(false));
  root.querySelector("[data-place-chat-minimize]").addEventListener("click", () => setOpen(false));
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) setOpen(false);
  });

  const placeLink = (place) => {
    const row = document.createElement("li");
    const link = document.createElement("a");
    link.className = "place-chat-card";
    link.href = place.url;
    if (root.dataset.chatMode === "guide") {
      link.addEventListener("click", (event) => {
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
          return;
        const guide = document.querySelector("[data-guide-root]");
        const card = Array.from(guide?.querySelectorAll("[data-place-card]") || []).find(
          (candidate) => candidate.dataset.placeId === place.id,
        );
        if (!card) return;
        event.preventDefault();
        if (window.matchMedia("(max-width: 640px)").matches) setOpen(false);
        guide.dispatchEvent(new CustomEvent("guide:show-place", { detail: { placeId: place.id } }));
      });
    }
    const photoUrl = typeof place.photo_url === "string" ? place.photo_url : "";
    if (
      root.dataset.showPhotos === "true" &&
      (/^\/(?!\/)/.test(photoUrl) || /^https:\/\//i.test(photoUrl))
    ) {
      const photo = document.createElement("img");
      photo.className = "place-chat-card-photo";
      photo.src = photoUrl;
      photo.alt = "";
      photo.loading = "lazy";
      photo.addEventListener("error", () => photo.remove());
      link.append(photo);
    }
    const content = document.createElement("div");
    content.className = "place-chat-card-content";
    const title = document.createElement("strong");
    title.className = "place-chat-card-title";
    title.textContent = place.name;
    const detail = document.createElement("span");
    detail.className = "place-chat-card-detail";
    detail.textContent = [place.neighborhood || place.city, place.category]
      .filter(Boolean)
      .join(" · ");
    content.append(title, detail);
    const stats = [];
    if (root.dataset.showRating === "true" && Number.isFinite(place.rating)) {
      let rating = `${place.rating.toFixed(1)} ★`;
      if (root.dataset.showReviews === "true" && Number.isFinite(place.user_rating_count)) {
        rating += ` (${new Intl.NumberFormat().format(place.user_rating_count)})`;
      }
      stats.push(rating);
    }
    if (root.dataset.showPrice === "true" && place.price_range) stats.push(place.price_range);
    if (stats.length) {
      const metadata = document.createElement("span");
      metadata.className = "place-chat-card-stats";
      metadata.textContent = stats.join(" · ");
      content.append(metadata);
    }
    if (root.dataset.showDescription === "true" && place.note) {
      const description = document.createElement("span");
      description.className = "place-chat-card-description";
      description.textContent = place.note;
      content.append(description);
    }
    link.append(content);
    row.append(link);
    return row;
  };

  const addMessage = (role, content, result = {}) => {
    if (result.guides?.length || result.places?.length || result.actions?.length)
      root.dataset.hasResults = "true";
    const item = document.createElement("div");
    item.className = `place-chat-message place-chat-message--${role}`;
    const copy = document.createElement("p");
    copy.textContent = content;
    item.append(copy);

    if (result.guides?.length) {
      const list = document.createElement("ul");
      list.className = "place-chat-guides";
      for (const guide of result.guides) {
        const row = document.createElement("li");
        const link = document.createElement("a");
        link.href = guide.url;
        link.textContent = guide.title;
        row.append(link);
        if (guide.places?.length) {
          const places = document.createElement("ul");
          places.className = "place-chat-places";
          guide.places.forEach((place) => places.append(placeLink(place)));
          row.append(places);
        }
        list.append(row);
      }
      item.append(list);
    } else if (result.places?.length) {
      const list = document.createElement("ul");
      list.className = "place-chat-places";
      result.places.forEach((place) => list.append(placeLink(place)));
      item.append(list);
    }

    if (root.dataset.chatMode === "guide" && Array.isArray(result.actions)) {
      const guide = document.querySelector("[data-guide-root]");
      const tags = JSON.parse(guide?.dataset.allTags || "[]");
      for (const proposed of result.actions.slice(0, 1)) {
        if (
          proposed?.id !== APPLY_FILTERS_ACTION ||
          proposed.version !== 1 ||
          proposed.guide_slug !== root.dataset.guideSlug
        )
          continue;
        const action = createFilterAction(proposed.filters, {
          guideSlug: root.dataset.guideSlug,
          tags,
          scope: proposed.scope,
          placeIds: Array.from(guide?.querySelectorAll("[data-place-card]") || []).map(
            (card) => card.dataset.placeId,
          ),
        });
        if (!action) continue;
        const box = document.createElement("div");
        box.className = "place-chat-filter-action";
        const label = document.createElement("p");
        label.textContent = "Filter this guide";
        const chips = document.createElement("div");
        chips.className = "place-chat-filter-chips";
        for (const text of [
          ...(action.scope ? [`Near: ${action.scope.label}`] : []),
          ...filterActionLabels(action.filters),
        ]) {
          const chip = document.createElement("span");
          chip.textContent = text;
          chips.append(chip);
        }
        const apply = document.createElement("button");
        apply.type = "button";
        apply.className = "place-chat-apply-filters";
        apply.textContent = "Apply filters";
        apply.addEventListener("click", () => {
          guide.dispatchEvent(new CustomEvent("guide:apply-filters", { detail: { action } }));
        });
        box.append(label, chips, apply);
        item.append(box);
      }
    }
    thread.append(item);
    thread.scrollTop = thread.scrollHeight;
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const content = input.value.trim();
    if (!content || submit.disabled) return;

    const nextMessages = [...messages, { role: "user", content }].slice(-7);
    addMessage("user", content);
    input.value = "";
    submit.disabled = true;
    status.textContent =
      root.dataset.chatMode === "guide" ? "Finding places..." : "Finding guides...";

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: root.dataset.chatMode,
          guide_slug: root.dataset.chatMode === "guide" ? root.dataset.guideSlug : undefined,
          messages: nextMessages,
          search_context: searchContext,
        }),
      });
      const result = await response.json();
      if (!response.ok && result.code === "ai_daily_quota_exhausted") {
        addMessage("assistant", result.error);
        input.value = content;
        const resetAt = Date.parse(result.resets_at);
        if (Number.isFinite(resetAt) && resetAt > Date.now() && resetAt - Date.now() <= 86400000) {
          quotaResetAt = resetAt;
          const resetLabel = new Intl.DateTimeFormat(undefined, {
            hour: "numeric",
            minute: "2-digit",
            timeZoneName: "short",
          }).format(new Date(resetAt));
          status.textContent = `Try chat again after ${resetLabel}. Search and filters still work.`;
          window.setTimeout(() => {
            quotaResetAt = 0;
            submit.disabled = false;
            status.textContent = "You can try chat again. Search and filters are always available.";
          }, resetAt - Date.now());
        }
        return;
      }
      if (!response.ok) throw new Error(result.error || "Chat is unavailable.");
      if (result.outcome !== "redirect") searchContext = result.search_context;

      messages.splice(0, messages.length, ...nextMessages, {
        role: "assistant",
        content: result.reply,
      });
      addMessage("assistant", result.reply, result);
      status.textContent = "";
    } catch (error) {
      addMessage("assistant", error.message || "Chat is unavailable. Please try again.");
      input.value = content;
      status.textContent = "";
    } finally {
      submit.disabled = quotaResetAt > Date.now();
      if (!panel.hidden && !submit.disabled) input.focus();
    }
  });
}
