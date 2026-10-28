const DEFAULT_CLOUDFLARE_MODEL = "@cf/zai-org/glm-4.7-flash";

export async function completeChat(env, messages) {
  const provider = env.CHAT_PROVIDER || "cloudflare";
  const model = env.CHAT_MODEL || DEFAULT_CLOUDFLARE_MODEL;
  const request = {
    messages,
    max_tokens: 320,
    response_format: { type: "json_object" },
  };

  if (provider === "cloudflare") {
    if (!env.AI?.run) throw new Error("Workers AI binding is missing");
    const cloudflareRequest =
      model === DEFAULT_CLOUDFLARE_MODEL
        ? { ...request, chat_template_kwargs: { enable_thinking: false } }
        : request;
    const result = await env.AI.run(model, cloudflareRequest);
    return extractContent(result);
  }

  if (provider === "openai-compatible") {
    if (!env.CHAT_API_BASE_URL || !env.CHAT_API_KEY || !env.CHAT_MODEL) {
      throw new Error("Chat API configuration is incomplete");
    }
    const url = new URL("chat/completions", `${env.CHAT_API_BASE_URL.replace(/\/$/, "")}/`);
    const response = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.CHAT_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ...request, model }),
      signal: AbortSignal.timeout(12000),
    });
    if (!response.ok) throw new Error(`Chat provider returned ${response.status}`);
    return extractContent(await response.json());
  }

  throw new Error(`Unsupported chat provider: ${provider}`);
}

function extractContent(result) {
  const content = result?.choices?.[0]?.message?.content ?? result?.response;
  if (typeof content === "string") return content;
  if (content && typeof content === "object" && !Array.isArray(content)) {
    return JSON.stringify(content);
  }
  if (Array.isArray(content)) {
    return content
      .filter((part) => part?.type === "text")
      .map((part) => part.text)
      .join("");
  }
  throw new Error("Chat provider returned no text");
}
