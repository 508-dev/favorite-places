const root = document.querySelector("[data-place-chat]");

if (root) {
  const thread = root.querySelector("[data-place-chat-thread]");
  const form = root.querySelector("[data-place-chat-form]");
  const input = root.querySelector("[data-place-chat-input]");
  const submit = root.querySelector("[data-place-chat-submit]");
  const status = root.querySelector("[data-place-chat-status]");
  const messages = [];

  const addMessage = (role, content, places = []) => {
    const item = document.createElement("div");
    item.className = `place-chat-message place-chat-message--${role}`;
    const copy = document.createElement("p");
    copy.textContent = content;
    item.append(copy);

    if (places.length > 0) {
      const list = document.createElement("ul");
      list.className = "place-chat-places";
      places.forEach((place) => {
        const row = document.createElement("li");
        const link = document.createElement("a");
        link.href = place.url;
        link.textContent = place.name;
        const detail = document.createElement("span");
        detail.textContent = [place.neighborhood || place.city, place.category]
          .filter(Boolean)
          .join(" · ");
        row.append(link, detail);
        list.append(row);
      });
      item.append(list);
    }

    thread.append(item);
    item.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const content = input.value.trim();
    if (!content || submit.disabled) return;

    const nextMessages = [...messages, { role: "user", content }].slice(-7);
    addMessage("user", content);
    input.value = "";
    submit.disabled = true;
    status.textContent = "Finding places...";

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: nextMessages }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Chat is unavailable.");

      messages.splice(0, messages.length, ...nextMessages, {
        role: "assistant",
        content: result.reply,
      });
      addMessage("assistant", result.reply, result.places || []);
      status.textContent = "";
    } catch (error) {
      addMessage("assistant", error.message || "Chat is unavailable. Please try again.");
      input.value = content;
      status.textContent = "";
    } finally {
      submit.disabled = false;
      input.focus();
    }
  });
}
