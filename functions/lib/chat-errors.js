export const AI_QUOTA_CODE = "ai_daily_quota_exhausted";

export function isDailyAiQuotaError(error, depth = 0) {
  if (!error || depth > 3) return false;
  // A bare 429/4006 can also mean transient capacity; require the documented
  // account-quota code or an explicit daily-allocation message.
  if (Number(error.code) === 3036) return true;
  const message = typeof error === "string" ? error : String(error.message || "");
  if (
    /used up your daily free allocation|daily (?:free )?(?:neuron|AI) (?:allowance|quota|allocation).*(?:exhausted|exceeded)/i.test(
      message,
    )
  )
    return true;
  return (
    isDailyAiQuotaError(error.cause, depth + 1) ||
    (Array.isArray(error.errors) &&
      error.errors.some((item) => isDailyAiQuotaError(item, depth + 1)))
  );
}

export function dailyQuotaDetails(now = new Date()) {
  const reset = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1));
  return {
    code: AI_QUOTA_CODE,
    error:
      "We've used today's AI search allowance. Chat should be available again after the daily reset at 00:00 UTC. You can still search and filter places normally.",
    resets_at: reset.toISOString(),
  };
}

export async function notifyQuotaExhausted(env) {
  if (env.CHAT_ALERTS_ENABLED !== "true" || !env.CHAT_ALERTS) return;
  try {
    const response = await env.CHAT_ALERTS.fetch(
      new Request("https://quota-alerts.internal/quota", {
        method: "POST",
        signal: AbortSignal.timeout(6000),
      }),
    );
    if (!response.ok) throw new Error(`Alert service returned ${response.status}`);
  } catch {
    console.error(JSON.stringify({ event: "chat.quota_alert_failed", code: AI_QUOTA_CODE }));
  }
}
